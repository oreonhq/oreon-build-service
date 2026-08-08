from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from oreon_build.worker.main import _clone_repo, _git_transient_failure


def test_git_transient_failure_detects_timeout_and_network():
    assert _git_transient_failure(124, "Timed out running: git clone")
    assert _git_transient_failure(128, "fatal: unable to access: Connection timed out")
    assert not _git_transient_failure(128, "fatal: repository not found")
    assert not _git_transient_failure(0, "")


def test_clone_repo_retries_timeouts_then_succeeds(tmp_path):
    calls = {"n": 0}

    def fake_run(cmd, timeout_s=None, cwd=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return 124, "Timed out running: git clone"
        Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        return 0, "cloned"

    with patch("oreon_build.worker.main._run", side_effect=fake_run), patch(
        "oreon_build.worker.main._sleep_with_jitter", return_value=None
    ):
        ok, out = _clone_repo("https://example.com/r.git", "main", tmp_path / "repo")

    assert ok is True
    assert calls["n"] == 3
    assert "attempt 1/11" in out
    assert "attempt 3/11" in out


def test_clone_repo_hard_fails_after_max_retries(tmp_path):
    def fake_run(cmd, timeout_s=None, cwd=None):
        return 124, "Timed out running: git clone"

    with patch("oreon_build.worker.main._run", side_effect=fake_run), patch(
        "oreon_build.worker.main._sleep_with_jitter", return_value=None
    ):
        ok, out = _clone_repo("https://example.com/r.git", None, tmp_path / "repo")

    assert ok is False
    assert out.count("git clone attempt") == 11
