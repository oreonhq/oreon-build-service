# Oreon Build Service
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Worker-facing API: poll job, heartbeat, upload RPMs for signing, report result."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from starlette.background import BackgroundTask
from sqlalchemy import select, func

from oreon_build.api.deps import DbSession
from oreon_build.config import get_settings
from oreon_build.core.security import verify_worker_token
from oreon_build.core.audit import log_audit
from sqlalchemy.orm import selectinload

from oreon_build.models import (
    Artifact,
    BuildAttempt,
    BuildJob,
    BuildStatus,
    BuildTarget,
    MockEnvironment,
    Package,
    Promotion,
    Release,
    Source,
    Worker,
    WorkerState,
)
from oreon_build.services.r2 import get_r2_client, log_r2_key, repo_r2_prefix, repo_rpms_key, src_r2_key
from oreon_build.services.publisher import update_repodata_at_prefix
from oreon_build.services.signing import sign_rpm
from oreon_build.services.worker_artifact_access import source_r2_keys_for_attempt
from oreon_build.services.worker_state_sync import sync_worker_state_after_attempt_finished

router = APIRouter(prefix="/worker", tags=["worker"])
logger = logging.getLogger(__name__)

_MAX_BUILD_LOG_BYTES = 64 * 1024 * 1024


def _pesign_material_configured(settings) -> bool:
    nss = Path((settings.pesign_nss_dir or "").strip()).expanduser()
    try:
        resolved = nss.resolve()
    except OSError:
        return False
    if not resolved.is_dir():
        return False
    if (settings.pesign_rules_json or "").strip():
        return True
    if (settings.pesign_certificate_nickname or "").strip():
        return True
    return False


def _build_pesign_bundle_file(dest: Path, nss_dir: Path, manifest: dict) -> None:
    raw = json.dumps(manifest, indent=2).encode("utf-8")
    with tarfile.open(dest, "w:gz") as tf:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        tf.addfile(info, io.BytesIO(raw))
        tf.add(str(nss_dir.resolve()), arcname="nss")


async def get_worker(
    db: DbSession,
    x_worker_token: Annotated[str | None, Header(alias="X-Worker-Token")] = None,
) -> Worker:
    if not x_worker_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-Worker-Token required")
    settings = get_settings()
    result = await db.execute(select(Worker))
    for w in result.scalars().all():
        if verify_worker_token(x_worker_token, settings.secret_key, w.token_hash):
            w.last_seen_at = datetime.now(timezone.utc)
            w.last_heartbeat = datetime.now(timezone.utc)
            await db.flush()
            return w
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token")


async def _require_worker(
    db: DbSession,
    x_worker_token: Annotated[str | None, Header(alias="X-Worker-Token")] = None,
) -> Worker:
    if not x_worker_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-Worker-Token required")
    settings = get_settings()
    result = await db.execute(select(Worker))
    for w in result.scalars().all():
        if verify_worker_token(x_worker_token, settings.secret_key, w.token_hash):
            w.last_seen_at = datetime.now(timezone.utc)
            w.last_heartbeat = datetime.now(timezone.utc)
            await db.flush()
            return w
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token")


class ResultPayload(BaseModel):
    status: str
    error_message: str | None = None
    log_r2_key: str | None = None
    artifacts: list[ArtifactPayload] = []


class ArtifactPayload(BaseModel):
    kind: str
    filename: str
    r2_key: str
    size_bytes: int | None = None
    checksum_sha256: str | None = None
    signed: bool | None = None


@router.post("/heartbeat")
async def heartbeat(db: DbSession, worker: Annotated[Worker, Depends(_require_worker)]):
    worker.last_heartbeat = datetime.now(timezone.utc)
    worker.last_seen_at = datetime.now(timezone.utc)
    if worker.state == WorkerState.OFFLINE:
        worker.state = WorkerState.IDLE
    await db.flush()
    return {"status": "ok", "state": worker.state.value}


@router.get("/poll")
async def poll_job(db: DbSession, worker: Annotated[Worker, Depends(_require_worker)]):
    if worker.state == WorkerState.OFFLINE:
        worker.state = WorkerState.IDLE
        await db.flush()
    if worker.state not in (WorkerState.IDLE, WorkerState.BUSY):
        return {"job": None}
    # Only return jobs whose target architecture matches this worker (or job has no target for backward compat)
    worker_arch = (worker.architecture or "").strip() or None
    q = (
        select(BuildJob)
        .where(BuildJob.status.in_([BuildStatus.PENDING, BuildStatus.QUEUED]))
        .order_by(BuildJob.priority.desc(), BuildJob.created_at.asc())
    )
    if worker_arch:
        q = q.outerjoin(BuildTarget, BuildJob.target_id == BuildTarget.id).where(
            (BuildJob.target_id.is_(None)) | (BuildTarget.architecture == worker_arch)
        )
    # One transaction must not claim the same job row twice (concurrent polls, duplicate worker
    # processes with the same token, or LB retry). Lock the chosen row until commit.
    q = q.limit(1).with_for_update(of=BuildJob, skip_locked=True)
    result = await db.execute(q)
    job = result.scalar_one_or_none()
    if not job:
        return {"job": None}
    job.status = BuildStatus.RUNNING
    count_result = await db.execute(
        select(func.count(BuildAttempt.id)).where(BuildAttempt.build_job_id == job.id)
    )
    attempt_number = (count_result.scalar() or 0) + 1
    attempt = BuildAttempt(
        build_job_id=job.id,
        worker_id=worker.id,
        attempt_number=attempt_number,
        status=BuildStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    db.add(attempt)
    worker.state = WorkerState.BUSY
    await db.flush()
    worker.current_build_attempt_id = attempt.id
    await db.flush()
    await db.refresh(attempt)

    releasename = None
    sources = []
    mock_config = None
    selected_mock_env: MockEnvironment | None = None
    target = None
    rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
    release = rel_result.scalar_one_or_none()
    if release:
        releasename = release.releasename
    # Collect sources: prefer this job's package_version so distgit/new-package sources are always included.
    if job.package_version_id:
        src_result = await db.execute(
            select(Source).where(Source.package_version_id == job.package_version_id)
        )
        for src in src_result.scalars().all():
            sources.append({"kind": src.kind, "r2_key": src.r2_key, "url": src.url})
    elif job.package_id:
        from oreon_build.models import PackageVersion as PV

        src_result = await db.execute(
            select(Source)
            .join(PV, Source.package_version_id == PV.id)
            .where(PV.package_id == job.package_id)
        )
        for src in src_result.scalars().all():
            sources.append({"kind": src.kind, "r2_key": src.r2_key, "url": src.url})
    if job.target_id:
        t_result = await db.execute(
            select(BuildTarget).where(BuildTarget.id == job.target_id).options(
                selectinload(BuildTarget.mock_environment)
            )
        )
        target = t_result.scalar_one_or_none()
        if target and target.mock_environment:
            selected_mock_env = target.mock_environment
            mock_config = selected_mock_env.config_name
    if not mock_config and job.package_id:
        pkg_result = await db.execute(
            select(Package).where(Package.id == job.package_id).options(
                selectinload(Package.preferred_mock_environment)
            )
        )
        pkg = pkg_result.scalar_one_or_none()
        if pkg and pkg.preferred_mock_environment and pkg.preferred_mock_environment.release_id == job.release_id:
            selected_mock_env = pkg.preferred_mock_environment
            mock_config = selected_mock_env.config_name
    if not mock_config and release:
        fallback = await db.execute(
            select(MockEnvironment).where(MockEnvironment.release_id == job.release_id).limit(1)
        )
        me = fallback.scalar_one_or_none()
        if me:
            selected_mock_env = me
            mock_config = me.config_name

    # At build time, worker uses <config_name>-<arch> (mock env is not arch-specific)
    build_arch = (target.architecture if target else None) or (worker.architecture or "").strip() or "x86_64"
    branch = getattr(job, "branch", None) or "dev"
    if mock_config:
        base = mock_config.strip()
        # Normalize: if stored value already ends with -x86_64 or -aarch64, treat as base
        for suffix in ("-x86_64", "-aarch64"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        mock_config = base + "-" + build_arch

    mock_additional_repos: list[str] = []
    if selected_mock_env and selected_mock_env.additional_repo_urls:
        mock_additional_repos = [str(u).strip() for u in selected_mock_env.additional_repo_urls if str(u).strip()]

    return {
        "job": {
            "build_job_id": job.id,
            "build_attempt_id": attempt.id,
            "package_id": job.package_id,
            "release_id": job.release_id,
            "target_id": job.target_id,
            "releasename": releasename,
            "branch": branch,
            "architecture": build_arch,
            "sources": sources,
            "mock_config": mock_config,
            "mock_additional_repos": mock_additional_repos,
        },
    }


@router.get("/cancel-check/{attempt_id}")
async def cancel_check(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
):
    """Worker polls this while a build runs; true when the user cancelled the job."""
    result = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    job_result = await db.execute(select(BuildJob).where(BuildJob.id == attempt.build_job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return {"cancel_requested": job.status == BuildStatus.CANCELLED}


def _put_rpm_bytes_to_r2(key: str, raw: bytes) -> None:
    get_r2_client().put_object(key, raw, content_type="application/x-rpm")


_RPM_UPLOAD_CONTENT_TYPE = "application/x-rpm"


async def _resolve_worker_rpm_upload_context(
    db: DbSession,
    worker: Worker,
    attempt_id: int,
    fname_raw: str,
) -> tuple[BuildAttempt, BuildJob, Release, str, str, str]:
    """
    Validate worker/attempt/job and compute repo ``r2_key`` for ``fname``.
    Returns ``attempt``, ``job``, ``release``, ``arch``, ``branch``, ``r2_key``.
    """
    fname = Path(fname_raw.strip()).name
    if not fname or not fname.lower().endswith(".rpm"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename must end with .rpm",
        )

    ar = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = ar.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")

    job_result = await db.execute(select(BuildJob).where(BuildJob.id == attempt.build_job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status == BuildStatus.CANCELLED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Job cancelled")

    rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
    release = rel_result.scalar_one_or_none()
    if not release:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Release missing",
        )

    arch = "x86_64"
    if job.target_id:
        t_result = await db.execute(select(BuildTarget).where(BuildTarget.id == job.target_id))
        t = t_result.scalar_one_or_none()
        if t and t.architecture:
            arch = t.architecture
    branch = getattr(job, "branch", None) or "dev"

    if fname.endswith(".src.rpm"):
        r2_key = src_r2_key(release.releasename, branch, fname)
    else:
        r2_key = repo_rpms_key(release.releasename, branch, arch, fname)

    return attempt, job, release, arch, branch, r2_key


class WorkerRpmPresignBody(BaseModel):
    filename: str


class WorkerRpmCompleteBody(BaseModel):
    filename: str
    checksum_sha256: str
    size_bytes: int


@router.post("/upload-rpm/presign/{attempt_id}")
async def upload_worker_rpm_presign(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    body: WorkerRpmPresignBody,
):
    """
    Presigned PUT to R2 so workers can upload very large RPMs without POSTing through Cloudflare Tunnel
    (which often returns 413 for very large bodies). Binary RPMs can still use multipart ``/upload-rpm/``.
    """
    settings = get_settings()
    _, _, _, _, _, r2_key = await _resolve_worker_rpm_upload_context(
        db, worker, attempt_id, body.filename
    )
    expires = settings.worker_rpm_presign_put_expires_seconds
    url = get_r2_client().generate_presigned_put_url(
        r2_key,
        content_type=_RPM_UPLOAD_CONTENT_TYPE,
        expires_in=expires,
    )
    await log_audit(
        db,
        action="worker.rpm_presign",
        resource_type="build_attempt",
        resource_id=str(attempt_id),
        details=f"worker_id={worker.id} key={r2_key}",
        emit_discord=False,
    )
    return {
        "put_url": url,
        "headers": {"Content-Type": _RPM_UPLOAD_CONTENT_TYPE},
        "expires_in": expires,
    }


@router.post("/upload-rpm/complete/{attempt_id}")
async def upload_worker_rpm_complete(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    body: WorkerRpmCompleteBody,
):
    """
    After the worker PUTs the unsigned RPM to R2 using a presigned URL, this runs on the controller:
    HEAD+GET from R2 into a temp file (same network path as any S3 client, not through the worker
    tunnel), verify size and SHA-256, ``rpm --addsign`` on that file, then ``put_object`` overwrites
    the same R2 key with the signed RPM bytes.
    """
    settings = get_settings()
    max_bytes = settings.max_worker_rpm_upload_mib * 1024 * 1024
    if body.size_bytes > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"RPM exceeds max {settings.max_worker_rpm_upload_mib} MiB",
        )

    _, _, _, _, _, r2_key = await _resolve_worker_rpm_upload_context(
        db, worker, attempt_id, body.filename
    )
    want_sha = (body.checksum_sha256 or "").strip().lower()
    if len(want_sha) != 64 or any(c not in "0123456789abcdef" for c in want_sha):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid checksum_sha256")
    sz = int(body.size_bytes)

    def _finalize() -> tuple[bytes, bool, str]:
        fd, tmp_path = tempfile.mkstemp(suffix=".rpm")
        os.close(fd)
        path = Path(tmp_path)
        try:
            r2 = get_r2_client()
            cl = r2.head_object_content_length(r2_key)
            if cl is None:
                raise FileNotFoundError("RPM not in R2 yet (complete presigned PUT first)")
            if cl != sz:
                raise ValueError(f"Size mismatch: R2 has {cl} bytes, worker claimed {sz}")
            stream = r2.get_object_stream(r2_key)
            if stream is None:
                raise FileNotFoundError("RPM not in R2")
            h = hashlib.sha256()
            try:
                with open(path, "wb") as out:
                    while True:
                        chunk = stream.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        h.update(chunk)
                        out.write(chunk)
            finally:
                stream.close()
            if h.hexdigest() != want_sha:
                raise ValueError("SHA256 of uploaded object does not match checksum_sha256")
            signed = sign_rpm(path)
            raw = path.read_bytes()
            r2.put_object(r2_key, raw, content_type=_RPM_UPLOAD_CONTENT_TYPE)
            return raw, signed, r2_key
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        raw, signed, rk = await asyncio.to_thread(_finalize)
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    digest = hashlib.sha256(raw).hexdigest()
    fname = Path(body.filename.strip()).name
    await log_audit(
        db,
        action="worker.rpm_upload_presigned_complete",
        resource_type="build_attempt",
        resource_id=str(attempt_id),
        details=f"worker_id={worker.id} {fname} {rk}",
        emit_discord=False,
    )
    return {
        "filename": fname,
        "r2_key": rk,
        "signed": signed,
        "pesign_applied": False,
        "size_bytes": len(raw),
        "checksum_sha256": digest,
    }


def _rpm_name_from_bytes(data: bytes) -> str | None:
    """Extract RPM NAME from bytes using host rpm command."""
    if not data:
        return None
    path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".rpm", delete=False) as f:
            f.write(data)
            path = f.name
        r = subprocess.run(
            ["rpm", "-qp", "--qf", "%{NAME}", path],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if r.returncode != 0:
            return None
        name = (r.stdout or "").strip()
        return name or None
    except Exception:
        return None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _prune_superseded_repo_objects(
    *,
    releasename: str,
    branch: str,
    arch: str,
    keep_artifact_filenames: set[str],
    payload_artifacts: list[ArtifactPayload],
) -> None:
    """
    Remove older RPM/SRPM objects in the same branch when a new release is published,
    including skipped-release upgrades (e.g. -11 -> -14).
    """
    r2 = get_r2_client()

    # Resolve package NAMEs from newly produced payload artifacts.
    binary_names: set[str] = set()
    src_names: set[str] = set()
    for a in payload_artifacts:
        fn = (a.filename or "").strip()
        if not fn.endswith(".rpm"):
            continue
        key = src_r2_key(releasename, branch, fn) if fn.endswith(".src.rpm") else repo_rpms_key(releasename, branch, arch, fn)
        data = r2.get_object(key)
        name = _rpm_name_from_bytes(data) if data else None
        if not name:
            continue
        if fn.endswith(".src.rpm"):
            src_names.add(name)
        else:
            binary_names.add(name)

    # Binary RPMs (same branch/arch)
    bin_prefix = f"{repo_r2_prefix(releasename, branch, arch)}/RPMS/"
    for obj in r2.list_objects(bin_prefix):
        key = obj.get("key")
        if not key:
            continue
        base = key.rsplit("/", 1)[-1]
        if not base.endswith(".rpm") or base.endswith(".src.rpm"):
            continue
        if base in keep_artifact_filenames:
            continue
        if not any(base.startswith(n + "-") for n in binary_names):
            continue
        data = r2.get_object(key)
        old_name = _rpm_name_from_bytes(data) if data else None
        if old_name and old_name in binary_names:
            r2.delete_object(key)
            logger.info("Pruned superseded binary RPM %s", key)

    # Source RPMs (same branch/src)
    src_prefix = f"{releasename}/{branch}/src/"
    for obj in r2.list_objects(src_prefix):
        key = obj.get("key")
        if not key:
            continue
        base = key.rsplit("/", 1)[-1]
        if not base.endswith(".src.rpm"):
            continue
        if base in keep_artifact_filenames:
            continue
        if not any(base.startswith(n + "-") for n in src_names):
            continue
        data = r2.get_object(key)
        old_name = _rpm_name_from_bytes(data) if data else None
        if old_name and old_name in src_names:
            r2.delete_object(key)
            logger.info("Pruned superseded SRPM %s", key)


_BUILD_LOG_PUT_CONTENT_TYPE = "text/plain; charset=utf-8"


async def _resolve_worker_build_log_r2_key(
    db: DbSession,
    worker: Worker,
    attempt_id: int,
) -> str:
    ar = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = ar.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")

    job_result = await db.execute(select(BuildJob).where(BuildJob.id == attempt.build_job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
    release = rel_result.scalar_one_or_none()
    if not release:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Release missing",
        )
    return log_r2_key(release.releasename, attempt_id)


class WorkerBuildLogCompleteBody(BaseModel):
    checksum_sha256: str
    size_bytes: int


@router.post("/build-log/presign/{attempt_id}")
async def worker_build_log_presign(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
):
    """
    Presigned PUT so workers can stream large logs to R2 without tunneling huge bodies through
    Cloudflare (which often returns 524 while the origin buffers and uploads to R2).
    """
    settings = get_settings()
    key = await _resolve_worker_build_log_r2_key(db, worker, attempt_id)
    expires = settings.worker_rpm_presign_put_expires_seconds
    url = get_r2_client().generate_presigned_put_url(
        key,
        content_type=_BUILD_LOG_PUT_CONTENT_TYPE,
        expires_in=expires,
    )
    await log_audit(
        db,
        action="worker.build_log_presign",
        resource_type="build_attempt",
        resource_id=str(attempt_id),
        details=f"worker_id={worker.id} key={key}",
        emit_discord=False,
    )
    return {
        "put_url": url,
        "headers": {"Content-Type": _BUILD_LOG_PUT_CONTENT_TYPE},
        "expires_in": expires,
    }


@router.post("/build-log/complete/{attempt_id}")
async def worker_build_log_complete(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    body: WorkerBuildLogCompleteBody,
):
    """After presigned PUT, verify log object in R2 (size + SHA-256). No re-upload; log is not GPG-signed."""
    if body.size_bytes > _MAX_BUILD_LOG_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Log exceeds max {_MAX_BUILD_LOG_BYTES // (1024 * 1024)} MiB",
        )
    key = await _resolve_worker_build_log_r2_key(db, worker, attempt_id)
    want_sha = (body.checksum_sha256 or "").strip().lower()
    if len(want_sha) != 64 or any(c not in "0123456789abcdef" for c in want_sha):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid checksum_sha256")
    sz = int(body.size_bytes)

    def _verify() -> str:
        r2 = get_r2_client()
        cl = r2.head_object_content_length(key)
        if cl is None:
            raise FileNotFoundError("Build log not in R2 yet (complete presigned PUT first)")
        if cl != sz:
            raise ValueError(f"Size mismatch: R2 has {cl} bytes, worker claimed {sz}")
        stream = r2.get_object_stream(key)
        if stream is None:
            raise FileNotFoundError("Build log not in R2")
        h = hashlib.sha256()
        try:
            while True:
                chunk = stream.read(8 * 1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        finally:
            stream.close()
        if h.hexdigest() != want_sha:
            raise ValueError("SHA256 of uploaded log does not match checksum_sha256")
        return key

    try:
        await asyncio.to_thread(_verify)
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    await log_audit(
        db,
        action="worker.build_log_presign_complete",
        resource_type="build_attempt",
        resource_id=str(attempt_id),
        details=f"worker_id={worker.id} {key}",
        emit_discord=False,
    )
    return {"log_r2_key": key}


@router.put("/build-log/{attempt_id}")
async def worker_upload_build_log(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    content: bytes = Body(...),
):
    """
    Store a build log in R2 on behalf of the authenticated worker. No R2 credentials on the worker.
    Large logs should use ``/build-log/presign`` + PUT to R2 + ``/build-log/complete`` from the worker instead.
    """
    if len(content) > _MAX_BUILD_LOG_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Log exceeds max {_MAX_BUILD_LOG_BYTES // (1024 * 1024)} MiB",
        )
    key = await _resolve_worker_build_log_r2_key(db, worker, attempt_id)

    def _put() -> None:
        get_r2_client().put_object(key, content, content_type=_BUILD_LOG_PUT_CONTENT_TYPE)

    await asyncio.to_thread(_put)
    return {"log_r2_key": key}


@router.get("/source-blob/{attempt_id}")
async def worker_download_source_blob(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    key: str = Query(..., min_length=1, max_length=2048),
):
    """Stream an object from R2 for keys attached to this job. Workers need no R2 creds."""
    ar = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    if not ar.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    allowed = await source_r2_keys_for_attempt(db, attempt_id)
    if key not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Object key not allowed for this attempt",
        )

    def _get() -> bytes | None:
        return get_r2_client().get_object(key)

    data = await asyncio.to_thread(_get)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
    return Response(content=data, media_type="application/octet-stream")


@router.get("/pesign-bundle/{attempt_id}")
async def worker_download_pesign_bundle(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
):
    """
    Tar.gz of NSS DB plus manifest. Only while attempt is RUNNING; only the assigned worker; use TLS.
    """
    settings = get_settings()
    ar = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = ar.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    if attempt.status != BuildStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Pesign bundle only while attempt is running",
        )
    if not _pesign_material_configured(settings):
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    nss_dir = Path(settings.pesign_nss_dir).expanduser().resolve()
    manifest = {
        "version": 1,
        "pesign_rules_json": settings.pesign_rules_json,
        "pesign_certificate_nickname": settings.pesign_certificate_nickname,
        "pesign_package_name_regex": settings.pesign_package_name_regex,
        "pesign_token": settings.pesign_token,
        "pesign_strict": settings.pesign_strict,
        "pesign_aarch64_qemu_prefix": settings.pesign_aarch64_qemu_prefix,
        "pesign_aarch64_guest_shell": settings.pesign_aarch64_guest_shell,
        "nss_subdir": "nss",
    }
    fd, tmp = tempfile.mkstemp(suffix=".tar.gz")
    os.close(fd)
    out_path = Path(tmp)

    def _build() -> None:
        _build_pesign_bundle_file(out_path, nss_dir, manifest)

    try:
        await asyncio.to_thread(_build)
    except Exception:
        out_path.unlink(missing_ok=True)
        raise

    await log_audit(
        db,
        action="worker.pesign_bundle_download",
        resource_type="build_attempt",
        resource_id=str(attempt_id),
        details=f"worker_id={worker.id}",
        emit_discord=False,
    )

    out_str = str(out_path)

    def _cleanup() -> None:
        try:
            os.unlink(out_str)
        except OSError:
            pass

    return FileResponse(
        path=out_str,
        filename=f"pesign-bundle-{attempt_id}.tar.gz",
        media_type="application/gzip",
        background=BackgroundTask(_cleanup),
    )


@router.post("/upload-rpm/{attempt_id}")
async def upload_worker_rpm(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    file: UploadFile = File(...),
):
    """
    Receive a built RPM. Pesign runs on the worker (bundle from GET .../pesign-bundle). GPG here.
    """
    settings = get_settings()
    max_bytes = settings.max_worker_rpm_upload_mib * 1024 * 1024

    fname_raw = file.filename or "package.rpm"
    _, _, _, _, _, r2_key = await _resolve_worker_rpm_upload_context(
        db, worker, attempt_id, fname_raw
    )
    fname = Path(fname_raw.strip()).name

    fd, tmp_path = tempfile.mkstemp(suffix=".rpm")
    os.close(fd)
    path = Path(tmp_path)
    raw: bytes = b""
    signed = False
    digest = ""
    r2_key = ""
    try:
        total = 0
        with open(path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"RPM exceeds max {settings.max_worker_rpm_upload_mib} MiB",
                    )
                out.write(chunk)

        signed = await asyncio.to_thread(sign_rpm, path)
        raw = await asyncio.to_thread(path.read_bytes)
        digest = hashlib.sha256(raw).hexdigest()

        await asyncio.to_thread(_put_rpm_bytes_to_r2, r2_key, raw)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("upload-rpm failed attempt_id=%s", attempt_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process RPM",
        ) from exc
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    return {
        "filename": fname,
        "r2_key": r2_key,
        "signed": signed,
        "pesign_applied": False,
        "size_bytes": len(raw),
        "checksum_sha256": digest,
    }


@router.post("/result/{attempt_id}")
async def report_result(
    attempt_id: int,
    payload: ResultPayload,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
):
    result = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    job_result = await db.execute(select(BuildJob).where(BuildJob.id == attempt.build_job_id))
    job = job_result.scalar_one()

    # User cancelled: do not mark job success or promote artifacts when the worker finishes later.
    if job.status == BuildStatus.CANCELLED:
        if payload.log_r2_key:
            attempt.log_r2_key = payload.log_r2_key
        if payload.error_message:
            prev = (attempt.error_message or "").strip()
            attempt.error_message = (prev + "\n" if prev else "") + payload.error_message.strip()
        if attempt.status == BuildStatus.RUNNING:
            attempt.status = BuildStatus.CANCELLED
            attempt.finished_at = datetime.now(timezone.utc)
        await db.flush()
        await sync_worker_state_after_attempt_finished(db, worker)
        await log_audit(
            db,
            action="build.result",
            account_id=None,
            resource_type="build_job",
            resource_id=str(job.id),
            details=f"status=ignored job_cancelled attempt_id={attempt_id}",
            ip_address=None,
            user_agent="worker",
        )
        return {"status": "ok", "job_cancelled": True}

    # Worker default HTTP read timeout can fire after this handler already committed rows. A later
    # orphan "failed" POST must not wipe success, and must fix failed status when artifacts exist.
    if str(payload.status).lower() == "failed":
        prior_art_count = await db.scalar(
            select(func.count(Artifact.id)).where(Artifact.build_attempt_id == attempt_id)
        )
        if (prior_art_count or 0) > 0:
            if attempt.status == BuildStatus.SUCCESS:
                logger.warning(
                    "Ignoring stale failed worker report for attempt %s (success + %d artifacts)",
                    attempt_id,
                    prior_art_count,
                )
                return {"status": "ok", "ignored_stale_failure": True}
            if attempt.status == BuildStatus.FAILED:
                logger.warning(
                    "Repairing attempt %s: failed report but %d artifacts (timeout race); restoring success",
                    attempt_id,
                    prior_art_count,
                )
                attempt.status = BuildStatus.SUCCESS
                attempt.error_message = None
                attempt.finished_at = datetime.now(timezone.utc)
                job.status = BuildStatus.SUCCESS
                job.completed_at = attempt.finished_at
                if payload.log_r2_key:
                    attempt.log_r2_key = payload.log_r2_key
                await db.flush()
                await sync_worker_state_after_attempt_finished(db, worker)
                promo_count = await db.scalar(
                    select(func.count(Promotion.id)).where(Promotion.build_job_id == job.id)
                )
                if (promo_count or 0) == 0:
                    pkg_result = await db.execute(select(Package).where(Package.id == job.package_id))
                    pkg = pkg_result.scalar_one_or_none()
                    db.add(
                        Promotion(
                            release_id=job.release_id,
                            from_channel="dev",
                            to_channel=None,
                            status="pending",
                            package_id=job.package_id,
                            package_name=pkg.name if pkg else None,
                            build_job_id=job.id,
                        )
                    )
                    await db.flush()
                rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
                release = rel_result.scalar_one_or_none()
                target_result = await db.execute(select(BuildTarget).where(BuildTarget.id == job.target_id))
                target = target_result.scalar_one_or_none()
                arch = target.architecture if target else None
                branch = getattr(job, "branch", "dev") or "dev"
                if release and arch:
                    try:
                        prefix = repo_r2_prefix(release.releasename, branch, arch)
                        update_repodata_at_prefix(prefix)
                    except Exception:
                        logger.exception("repodata refresh after repair attempt_id=%s", attempt_id)

                await log_audit(
                    db,
                    action="build.result",
                    account_id=None,
                    resource_type="build_job",
                    resource_id=str(job.id),
                    details=f"status=repaired_success attempt_id={attempt_id}",
                    ip_address=None,
                    user_agent="worker",
                )
                return {"status": "ok", "repaired_stale_failure": True}

    attempt.status = BuildStatus(payload.status)
    attempt.finished_at = datetime.now(timezone.utc)
    if payload.error_message:
        attempt.error_message = payload.error_message
    if payload.log_r2_key:
        attempt.log_r2_key = payload.log_r2_key
    job.status = BuildStatus(payload.status)
    job.completed_at = attempt.finished_at
    await db.flush()
    await sync_worker_state_after_attempt_finished(db, worker)
    for a in payload.artifacts:
        art = Artifact(
            build_attempt_id=attempt_id,
            kind=a.kind,
            filename=a.filename,
            r2_key=a.r2_key,
            size_bytes=a.size_bytes,
            checksum_sha256=a.checksum_sha256,
            is_signed=bool(getattr(a, "signed", False)),
        )
        db.add(art)
    await db.flush()

    # On success: ensure RPMs are in dev/, then update repodata, create promotion
    # Worker may upload to builds/ or dev/. If builds/: copy to dev/ FIRST, then we delete builds/ later.
    if payload.status == "success" and payload.artifacts:
        pkg_result = await db.execute(select(Package).where(Package.id == job.package_id))
        pkg = pkg_result.scalar_one_or_none()
        rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
        release = rel_result.scalar_one()
        target_result = await db.execute(select(BuildTarget).where(BuildTarget.id == job.target_id))
        target = target_result.scalar_one_or_none()
        arch = target.architecture if target else None
        branch = getattr(job, "branch", "dev") or "dev"
        if release and arch:
            try:
                r2 = get_r2_client()
                releasename = release.releasename
                # Copy any artifacts from builds/ to dev/ (worker may still use builds/)
                for a in payload.artifacts:
                    if a.r2_key and "/builds/" in a.r2_key:
                        data = r2.get_object(a.r2_key)
                        if data and a.filename:
                            if a.filename.endswith(".src.rpm"):
                                dest = src_r2_key(releasename, branch, a.filename)
                            else:
                                dest = repo_rpms_key(releasename, branch, arch, a.filename)
                            r2.put_object(dest, data)
                keep = {a.filename for a in payload.artifacts if a.filename}
                _prune_superseded_repo_objects(
                    releasename=releasename,
                    branch=branch,
                    arch=arch,
                    keep_artifact_filenames=keep,
                    payload_artifacts=payload.artifacts,
                )
                prefix = repo_r2_prefix(releasename, branch, arch)
                update_repodata_at_prefix(prefix)
            except Exception:
                logger.exception(
                    "Post-build repo publish/cleanup failed releasename=%s branch=%s arch=%s attempt_id=%s",
                    release.releasename if release else "?",
                    branch,
                    arch,
                    attempt_id,
                )
            # Copy package SRPM to releasename/branch/src/ (from uploads) if we have one
            if job.package_version_id:
                src_result = await db.execute(
                    select(Source).where(
                        Source.package_version_id == job.package_version_id,
                        Source.r2_key.isnot(None),
                        Source.r2_key != "",
                    ).limit(1)
                )
                src_row = src_result.scalar_one_or_none()
                if src_row and src_row.r2_key:
                    try:
                        r2 = get_r2_client()
                        data = r2.get_object(src_row.r2_key)
                        if data:
                            filename = src_row.r2_key.split("/")[-1]
                            if filename.endswith(".src.rpm"):
                                dest_key = src_r2_key(release.releasename, branch, filename)
                                r2.put_object(dest_key, data)
                    except Exception:
                        pass
        # Create promotion candidate (pending)
        promo = Promotion(
            release_id=job.release_id,
            from_channel="dev",
            to_channel=None,
            status="pending",
            package_id=job.package_id,
            package_name=pkg.name if pkg else None,
            build_job_id=job.id,
        )
        db.add(promo)
        await db.flush()

    # Delete upload R2 objects after build (success or fail); uploads are not reused for retries.
    # ONLY delete keys under /uploads/ - never touch repo path (dev/arch/RPMS, dev/src).
    if job.package_version_id:
        src_list_result = await db.execute(
            select(Source).where(
                Source.package_version_id == job.package_version_id,
                Source.r2_key.isnot(None),
                Source.r2_key != "",
            )
        )
        try:
            r2 = get_r2_client()
            for src in src_list_result.scalars().all():
                if src.r2_key and "/uploads/" in src.r2_key:
                    try:
                        r2.delete_object(src.r2_key)
                    except Exception:
                        pass
                src.r2_key = None
        except Exception:
            pass
        await db.flush()

    # Delete ONLY keys under releasename/builds/{attempt_id}/ - never touch dev/arch or repo path.
    rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
    release = rel_result.scalar_one_or_none()
    if release:
        exact_builds_prefix = f"{release.releasename}/builds/{attempt_id}/"
        art_keys_result = await db.execute(
            select(Artifact.r2_key).where(
                Artifact.build_attempt_id == attempt_id,
                Artifact.r2_key.isnot(None),
                Artifact.r2_key != "",
            )
        )
        try:
            r2 = get_r2_client()
            for (key,) in art_keys_result.all():
                if key and key.startswith(exact_builds_prefix):
                    try:
                        r2.delete_object(key)
                    except Exception:
                        pass
        except Exception:
            pass

    # Emit audit + Discord for build result
    await log_audit(
        db,
        action="build.result",
        account_id=None,
        resource_type="build_job",
        resource_id=str(job.id),
        details=f"status={payload.status} attempt_id={attempt_id}",
        ip_address=None,
        user_agent="worker",
    )

    return {"status": "ok"}


@router.post("/artifact/{attempt_id}")
async def register_artifact(
    attempt_id: int,
    payload: ArtifactPayload,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
):
    result = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    art = Artifact(
        build_attempt_id=attempt_id,
        kind=payload.kind,
        filename=payload.filename,
        r2_key=payload.r2_key,
        size_bytes=payload.size_bytes,
        checksum_sha256=payload.checksum_sha256,
    )
    db.add(art)
    await db.flush()
    return {"id": art.id}
