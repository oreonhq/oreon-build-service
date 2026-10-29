# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path


def test_kernel_srpm_isolated_from_reused_mock_root(monkeypatch, tmp_path: Path) -> None:
    from oreon_build.worker import main

    srpm = tmp_path / "kernel-7.2.3-200.or11.src.rpm"
    monkeypatch.setattr(main, "_rpm_query_value", lambda *_: "kernel")
    assert main._is_kernel_srpm(srpm) is True

    monkeypatch.setattr(main, "_rpm_query_value", lambda *_: "bash")
    assert main._is_kernel_srpm(srpm) is False


def test_kernel_module_vermagic_mismatch_is_rejected(monkeypatch, tmp_path: Path) -> None:
    from oreon_build.worker import main

    rpm = tmp_path / "kernel-modules-core-7.2.3-200.or11.x86_64.rpm"
    module = tmp_path / "nvme.ko.xz"
    module.touch()

    def query(_rpm: Path, fmt: str) -> str:
        if fmt == "%{NAME}":
            return "kernel-modules-core"
        return "7.2.3-200.or11.x86_64"

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def run(cmd, **_kwargs):
        if cmd[0] == "modinfo":
            return type("ModinfoResult", (), {"returncode": 0, "stdout": "7.1.3-200.or11.x86_64 SMP", "stderr": ""})()
        return Result()

    original_glob = Path.glob

    def glob(self: Path, pattern: str):
        if pattern == "usr/lib/modules/**/*.ko*":
            return [module]
        return original_glob(self, pattern)

    monkeypatch.setattr(main, "_rpm_query_value", query)
    monkeypatch.setattr(main.subprocess, "run", run)
    monkeypatch.setattr(Path, "glob", glob)

    errors = main._kernel_module_vermagic_errors(rpm)
    assert errors == [
        "nvme.ko.xz: vermagic '7.1.3-200.or11.x86_64 SMP', expected '7.2.3-200.or11.x86_64'"
    ]
