# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path

import httpx

from oreon_build.worker import main as worker_main


def test_rpm_upload_survives_temporary_controller_outage(monkeypatch, tmp_path: Path) -> None:
    rpm = tmp_path / "kernel-debug-debuginfo.rpm"
    rpm.write_bytes(b"rpm")
    calls = 0

    def upload(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("controller unavailable")
        return {"filename": rpm.name}

    monkeypatch.setattr(worker_main, "_upload_rpm_to_controller", upload)
    monkeypatch.setattr(worker_main, "_sleep_with_jitter", lambda delay: None)

    result = worker_main._upload_rpm_to_controller_resilient(
        httpx.Client(), "https://controller.invalid", 74003, rpm
    )

    assert result == {"filename": rpm.name}
    assert calls == 3


def test_rpm_upload_does_not_retry_permanent_error(monkeypatch, tmp_path: Path) -> None:
    rpm = tmp_path / "bad.rpm"
    rpm.write_bytes(b"rpm")
    request = httpx.Request("POST", "https://controller.invalid/complete")
    response = httpx.Response(400, request=request)
    calls = 0

    def upload(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.HTTPStatusError("bad RPM", request=request, response=response)

    monkeypatch.setattr(worker_main, "_upload_rpm_to_controller", upload)

    try:
        worker_main._upload_rpm_to_controller_resilient(
            httpx.Client(), "https://controller.invalid", 74003, rpm
        )
    except httpx.HTTPStatusError:
        pass
    else:
        raise AssertionError("permanent upload error was not raised")

    assert calls == 1
