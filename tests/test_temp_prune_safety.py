#
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Temp prune safety tests for oreon worker reliability."""

import os
import time
from pathlib import Path

import pytest


def _set_mtime(p: Path, age_sec: int) -> None:
    """Force mtime older than now - age_sec."""
    now = time.time()
    old = now - age_sec
    os.utime(str(p), (old, old))


def test_prune_skips_inflight_attempt_dirs(tmp_path, monkeypatch):
    # Use a deterministic temp parent so we do not touch system /tmp.
    monkeypatch.setenv("OREON_WORKER_TMP_PARENT", str(tmp_path))
    monkeypatch.setenv("OREON_WORKER_PRUNE_TMP_AGE_SEC", "5")

    from oreon_build.worker import main as worker_main

    inflight_id = 123
    other_id = 456

    keep_dir = tmp_path / f"oreon-worker-{inflight_id}-keep"
    drop_dir = tmp_path / f"oreon-worker-{other_id}-drop"
    keep_dir.mkdir(parents=True, exist_ok=True)
    drop_dir.mkdir(parents=True, exist_ok=True)

    _set_mtime(keep_dir, age_sec=10)
    _set_mtime(drop_dir, age_sec=10)

    # Mark keep attempt as inflight so pruning must not delete it.
    with worker_main._inflight_attempt_lock:
        worker_main._inflight_attempt_ids.add(inflight_id)

    try:
        worker_main._prune_stale_oreon_worker_tempdirs()
        assert keep_dir.exists(), "in-flight attempt dir got pruned"
        assert not drop_dir.exists(), "old non-inflight attempt dir should be pruned"
    finally:
        with worker_main._inflight_attempt_lock:
            worker_main._inflight_attempt_ids.discard(inflight_id)


def test_prune_skips_active_path_registered_dir(tmp_path, monkeypatch):
    """Active-path set protects a dir even when attempt-id is NOT in the inflight set.

    This covers the scenario where the prune runs from a parallel job's cleanup while the
    inflight-id snapshot was taken between the id being removed and the path being removed,
    or when two separate worker processes share the same TMPDIR.
    """
    monkeypatch.setenv("OREON_WORKER_TMP_PARENT", str(tmp_path))
    monkeypatch.setenv("OREON_WORKER_PRUNE_TMP_AGE_SEC", "5")

    from oreon_build.worker import main as worker_main

    active_dir = tmp_path / "oreon-worker-789-activepath"
    drop_dir = tmp_path / "oreon-worker-999-shoulddrop"
    active_dir.mkdir(parents=True, exist_ok=True)
    drop_dir.mkdir(parents=True, exist_ok=True)

    _set_mtime(active_dir, age_sec=10)
    _set_mtime(drop_dir, age_sec=10)

    # Register path only (NOT in inflight attempt set) to simulate the timing window or
    # a cross-process scenario where the in-memory set does not have the attempt-id.
    with worker_main._inflight_attempt_lock:
        worker_main._active_work_tmpdir_paths.add(str(active_dir))

    try:
        worker_main._prune_stale_oreon_worker_tempdirs()
        assert active_dir.exists(), "active-path-registered dir got pruned despite being live"
        assert not drop_dir.exists(), "old unregistered dir should be pruned"
    finally:
        with worker_main._inflight_attempt_lock:
            worker_main._active_work_tmpdir_paths.discard(str(active_dir))


def test_prune_skips_sentinel_file_dir(tmp_path, monkeypatch):
    """The .oreon-active sentinel file inside the dir protects it cross-process."""
    monkeypatch.setenv("OREON_WORKER_TMP_PARENT", str(tmp_path))
    monkeypatch.setenv("OREON_WORKER_PRUNE_TMP_AGE_SEC", "5")

    from oreon_build.worker import main as worker_main

    sentinel_dir = tmp_path / "oreon-worker-111-sentinel"
    drop_dir = tmp_path / "oreon-worker-222-nosent"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    drop_dir.mkdir(parents=True, exist_ok=True)

    (sentinel_dir / ".oreon-active").write_text("12345", encoding="utf-8")

    _set_mtime(sentinel_dir, age_sec=10)
    _set_mtime(drop_dir, age_sec=10)

    # Neither dir is in the in-memory sets (simulating a different process wrote the sentinel).
    worker_main._prune_stale_oreon_worker_tempdirs()
    assert sentinel_dir.exists(), "dir with .oreon-active sentinel got pruned"
    assert not drop_dir.exists(), "dir without sentinel should be pruned"

