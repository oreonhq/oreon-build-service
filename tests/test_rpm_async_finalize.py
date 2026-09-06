import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from oreon_build.worker import main as worker_main
from oreon_build.api import worker_api


@pytest.mark.parametrize("reply", [202, 524, "timeout", 200])
def test_complete_polls_without_reupload(tmp_path, monkeypatch, reply):
    rpm = tmp_path / "kernel-debug-debuginfo-7.2.3-200.or11.aarch64.rpm"
    rpm.write_bytes(b"rpm")
    calls = []
    result = {"filename": rpm.name, "r2_key": "repo/kernel.rpm", "signed": True,
              "pesign_applied": False, "size_bytes": 4, "checksum_sha256": "a" * 64}

    def handle(request):
        calls.append(request)
        if "/presign/" in request.url.path:
            return httpx.Response(200, json={"put_url": "https://r2.invalid/rpm"})
        if request.method == "PUT":
            return httpx.Response(200)
        if "/complete/" in request.url.path:
            assert request.headers["Prefer"] == "respond-async"
            if reply == "timeout":
                raise httpx.ReadTimeout("proxy disconnected", request=request)
            return httpx.Response(reply, json=result if reply == 200 else {})
        if not any("/complete/" in r.url.path for r in calls):
            return httpx.Response(200, json={"finalized": False})
        return httpx.Response(200, json={"finalized": True, **result})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert worker_main._upload_rpm_via_presigned_r2(client, "https://controller.invalid", 74, rpm) == result
    assert sum(r.method == "PUT" for r in calls) == 1
    assert sum("/complete/" in r.url.path for r in calls) == 1


async def test_async_complete_returns_before_signing_and_deduplicates(monkeypatch):
    from oreon_build import db as db_module

    entered = asyncio.Event()
    release = asyncio.Event()
    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=9)), commit=AsyncMock())

    class Session:
        async def __aenter__(self):
            entered.set()
            await release.wait()
            return session

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(db_module, "async_session_maker", Session)
    monkeypatch.setattr(worker_api, "_resolve_worker_rpm_upload_context", AsyncMock())
    body = worker_api.WorkerRpmCompleteBody(filename="kernel.aarch64.rpm", checksum_sha256="a" * 64, size_bytes=3)
    handler = worker_api.upload_worker_rpm_complete
    response = await handler(74, SimpleNamespace(rollback=AsyncMock()), SimpleNamespace(id=9), body, prefer="respond-async")
    assert response.status_code == 202
    await entered.wait()
    task = worker_api._rpm_finalize_tasks[(74, body.filename)]
    response = await handler(74, SimpleNamespace(rollback=AsyncMock()), SimpleNamespace(id=9), body, prefer="respond-async")
    assert response.status_code == 202
    assert worker_api._rpm_finalize_tasks[(74, body.filename)] is task
    finalize = AsyncMock(return_value={})
    monkeypatch.setattr(worker_api, "_finalize_worker_rpm", finalize)
    release.set()
    await task
    finalize.assert_awaited_once_with(74, session, session.get.return_value, body)
    session.commit.assert_awaited_once()
    assert (74, body.filename) not in worker_api._rpm_finalize_tasks


@pytest.mark.parametrize("finalized", [False, True])
def test_retry_preserves_existing_r2_object(tmp_path, finalized):
    rpm = tmp_path / "kernel.aarch64.rpm"
    rpm.write_bytes(b"rpm")
    paths = []

    def handle(request):
        paths.append(request.url.path)
        assert request.method != "PUT"
        assert "/presign/" not in request.url.path
        if "/complete/" in request.url.path:
            return httpx.Response(200, json={"filename": rpm.name})
        return httpx.Response(200, json={"finalized": finalized, "finalizing": not finalized, "filename": rpm.name})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = worker_main._upload_rpm_via_presigned_r2(client, "https://controller.invalid", 74, rpm)
    assert result["filename"] == rpm.name
    assert len(paths) == (1 if finalized else 2)


@pytest.mark.parametrize("sha,size,code", [("bad", 3, 400), ("a" * 64, -1, 400), ("a" * 64, 2**60, 413)])
async def test_async_complete_rejects_invalid_input(monkeypatch, sha, size, code):
    monkeypatch.setattr(worker_api, "_resolve_worker_rpm_upload_context", AsyncMock())
    body = worker_api.WorkerRpmCompleteBody(filename="kernel.rpm", checksum_sha256=sha, size_bytes=size)
    with pytest.raises(worker_api.HTTPException) as exc:
        await worker_api.upload_worker_rpm_complete(74, object(), SimpleNamespace(id=9), body, prefer="respond-async")
    assert exc.value.status_code == code
    assert not worker_api._rpm_finalize_tasks


async def test_complete_reuses_committed_artifact_under_lock(monkeypatch):
    artifact = {"finalized": True, "filename": "kernel.rpm", "checksum_sha256": "b" * 64}
    monkeypatch.setattr(worker_api, "_resolve_worker_rpm_upload_context", AsyncMock(return_value=(None, None, None, None, None, "repo/kernel.rpm")))
    monkeypatch.setattr(worker_api, "upload_worker_rpm_status", AsyncMock(return_value=artifact))
    monkeypatch.setattr(worker_api, "get_r2_client", lambda: pytest.fail("must not sign or download again"))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: "owner")),
        commit=AsyncMock(), rollback=AsyncMock(),
    )
    body = worker_api.WorkerRpmCompleteBody(filename="kernel.rpm", checksum_sha256="a" * 64, size_bytes=3)
    assert await worker_api._finalize_worker_rpm(74, db, SimpleNamespace(id=9), body) == artifact
    assert db.execute.await_count == 2  # Acquire and release the database lock.
    assert db.commit.await_count == 2


@pytest.mark.parametrize("fail", [False, True])
async def test_legacy_completion_streams_until_finalization(monkeypatch, fail):
    import json
    from oreon_build import db as db_module

    ready = asyncio.Event()
    result = {"filename": "kernel.rpm", "signed": True, "checksum_sha256": "b" * 64}

    async def finalize(*args):
        await ready.wait()
        if fail:
            raise RuntimeError("signing failed")
        return result

    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=9)), commit=AsyncMock())

    class Session:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(db_module, "async_session_maker", Session)
    monkeypatch.setattr(worker_api, "_finalize_worker_rpm", finalize)
    monkeypatch.setattr(worker_api, "_resolve_worker_rpm_upload_context", AsyncMock())
    body = worker_api.WorkerRpmCompleteBody(filename="kernel.rpm", checksum_sha256="a" * 64, size_bytes=3)
    response = await worker_api.upload_worker_rpm_complete(74, SimpleNamespace(rollback=AsyncMock()), SimpleNamespace(id=9), body)
    assert response.headers["x-accel-buffering"] == "no"
    chunks = response.body_iterator
    first = await anext(chunks)
    assert first == b" "  # Response starts before slow signing finishes.
    task = worker_api._rpm_finalize_tasks[(74, body.filename)]
    ready.set()
    if fail:
        with pytest.raises(RuntimeError, match="signing failed"):
            await anext(chunks)
    else:
        assert json.loads(first + await anext(chunks)) == result
        session.commit.assert_awaited_once()
    await asyncio.sleep(0)
    assert task.done()
    assert (74, body.filename) not in worker_api._rpm_finalize_tasks
