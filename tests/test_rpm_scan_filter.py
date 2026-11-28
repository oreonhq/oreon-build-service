# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.exc import InterfaceError

from oreon_build.api.routers.auth import _login_hits, login_rate_limited
from oreon_build.core.scratch import disk_tmpdir
from oreon_build.models import BuildStatus
from oreon_build.services import rpm_scan
from oreon_build.services.rpm_scan import parse_clam_line, parse_yr_json, skip_scan_path, _scan_failure


def test_skip_noise_paths():
    assert skip_scan_path("usr/share/doc/foo/README")
    assert skip_scan_path("usr/lib/tests/payload")
    assert skip_scan_path("opt/pkg/testdata/sample")
    assert skip_scan_path("usr/share/rules/a.yar")
    assert not skip_scan_path("usr/bin/curl")


def test_disk_tmpdir_refuses_tmpfs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OREON_DISK_TMPDIR", "/tmp/oreon-nope")
    monkeypatch.setenv("OREON_SCAN_TMPDIR", "/tmp/oreon-nope")
    monkeypatch.setenv("OREON_REPO_TMPDIR", "/tmp/oreon-nope")
    got = disk_tmpdir("scan")
    text = str(got.resolve())
    assert not text.startswith("/tmp/")
    assert got.is_dir()


def test_clam_ignores_pua_and_doc(tmp_path: Path):
    root = tmp_path
    hit = root / "usr" / "bin" / "bad"
    hit.parent.mkdir(parents=True)
    hit.write_text("x")
    doc = root / "usr" / "share" / "doc" / "pkg" / "note"
    doc.parent.mkdir(parents=True)
    doc.write_text("x")
    assert parse_clam_line(f"{hit}: Win.Trojan.Agent FOUND", root) == ("usr/bin/bad", "Win.Trojan.Agent")
    assert parse_clam_line(f"{doc}: Win.Trojan.Agent FOUND", root) is None
    assert parse_clam_line(f"{hit}: PUA.Win.Trojan FOUND", root) is None
    assert parse_clam_line(f"{hit}: Heuristics.Encrypted FOUND", root) is None


def test_yr_only_keeps_our_rules():
    raw = """
    [{"path": "/tmp/scripts.txt", "rules": [
      {"identifier": "oreon_curl_or_wget_piped_to_shell", "matches": [{"data": "curl x | sh"}]},
      {"identifier": "generic_packer", "matches": [{"data": "upx"}]}
    ]}]
    """
    rows = parse_yr_json(raw)
    assert len(rows) == 1
    assert rows[0][0] == "oreon_curl_or_wget_piped_to_shell"
    assert "curl x | sh" in rows[0][2]


def test_dead_clamd_is_not_a_walk_error():
    text = "\n".join([
        "WARNING: Ignoring unsupported option --recursive (-r)",
        "ERROR: Could not connect to clamd on LocalSocket /tmp/clamd.sock: No such file or directory",
        "LibClamAV Error: File tree walk aborted.",
    ])
    assert _scan_failure(text) == "clamd is not running"


def test_scan_does_not_hold_db_while_reading_rpms(monkeypatch, tmp_path: Path):
    asyncio.run(_scan_does_not_hold_db_while_reading_rpms(monkeypatch, tmp_path))


async def _scan_does_not_hold_db_while_reading_rpms(monkeypatch, tmp_path: Path):
    open_count = {"n": 0}
    seen = {"during": []}
    job = SimpleNamespace(id=7, status=BuildStatus.SUCCESS, scan_state="pending")
    art = SimpleNamespace(filename="kernel-debuginfo.rpm", r2_key="builds/kernel-debuginfo.rpm")

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [art]

    class DB:
        async def get(self, model, pk):
            return job

        async def execute(self, stmt):
            return Result()

        def add(self, obj):
            return None

        async def commit(self):
            return None

    db = DB()

    class Session:
        async def __aenter__(self):
            open_count["n"] += 1
            return db

        async def __aexit__(self, *args):
            open_count["n"] -= 1

    def download(key, dest):
        seen["during"].append(open_count["n"])
        dest.write_bytes(b"rpm")
        return None

    def scan_path(path):
        seen["during"].append(open_count["n"])
        return [{"engine": "clamav", "rule": "Win.Test", "file_path": "usr/bin/x", "evidence": "x"}], None

    monkeypatch.setattr(rpm_scan, "async_session_maker", Session)
    monkeypatch.setattr(rpm_scan, "_download", download)
    monkeypatch.setattr(rpm_scan, "scan_rpm_path", scan_path)
    monkeypatch.setattr(rpm_scan, "_scan_tmp_parent", lambda: tmp_path)
    monkeypatch.setattr(rpm_scan, "log_audit", _noop_audit)

    await rpm_scan._scan_job(7, 11)
    assert seen["during"] == [0, 0]
    assert job.scan_state == "quarantined"
    assert open_count["n"] == 0


def test_scan_retries_dead_db_write(monkeypatch, tmp_path: Path):
    asyncio.run(_scan_retries_dead_db_write(monkeypatch, tmp_path))


async def _scan_retries_dead_db_write(monkeypatch, tmp_path: Path):
    job = SimpleNamespace(id=8, status=BuildStatus.SUCCESS, scan_state="pending")
    art = SimpleNamespace(filename="kernel.rpm", r2_key="builds/kernel.rpm")
    commits = {"n": 0}

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [art]

    class DB:
        def __init__(self, dead: bool):
            self.dead = dead

        async def get(self, model, pk):
            if self.dead:
                raise InterfaceError("connection is closed", None, None)
            return job

        async def execute(self, stmt):
            if self.dead:
                raise InterfaceError("connection is closed", None, None)
            return Result()

        def add(self, obj):
            return None

        async def commit(self):
            commits["n"] += 1

    made = {"n": 0}

    class Session:
        async def __aenter__(self):
            made["n"] += 1
            return DB(dead=made["n"] == 2)

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(rpm_scan, "async_session_maker", Session)
    def download(key, dest):
        dest.write_bytes(b"rpm")
        return None

    monkeypatch.setattr(rpm_scan, "_download", download)
    monkeypatch.setattr(rpm_scan, "scan_rpm_path", lambda path: ([], None))
    monkeypatch.setattr(rpm_scan, "_scan_tmp_parent", lambda: tmp_path)

    await rpm_scan._scan_job(8, 12)
    assert job.scan_state == "clean"
    assert commits["n"] == 1
    assert made["n"] == 3


async def _noop_audit(*args, **kwargs):
    return None


def test_login_rate_limit_trips():
    _login_hits.clear()
    ip = "203.0.113.9"
    assert all(login_rate_limited(ip, now=1000 + i) is False for i in range(12))
    assert login_rate_limited(ip, now=1012) is True
    _login_hits.clear()
