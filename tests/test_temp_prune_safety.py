import os
import time
from pathlib import Path

import pytest

def _set_mtime(p: Path, age_sec: int) -> None:
    now = time.time()
    old = now - age_sec
    os.utime(str(p), (old, old))

def test_prune_skips_inflight_attempt_dirs(tmp_path, monkeypatch):
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
    monkeypatch.setenv("OREON_WORKER_TMP_PARENT", str(tmp_path))
    monkeypatch.setenv("OREON_WORKER_PRUNE_TMP_AGE_SEC", "5")

    from oreon_build.worker import main as worker_main

    active_dir = tmp_path / "oreon-worker-789-activepath"
    drop_dir = tmp_path / "oreon-worker-999-shoulddrop"
    active_dir.mkdir(parents=True, exist_ok=True)
    drop_dir.mkdir(parents=True, exist_ok=True)

    _set_mtime(active_dir, age_sec=10)
    _set_mtime(drop_dir, age_sec=10)

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

    worker_main._prune_stale_oreon_worker_tempdirs()
    assert sentinel_dir.exists(), "dir with .oreon-active sentinel got pruned"
    assert not drop_dir.exists(), "dir without sentinel should be pruned"

