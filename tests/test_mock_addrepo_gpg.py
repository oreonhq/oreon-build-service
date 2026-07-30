from pathlib import Path

from oreon_build.worker.main import _write_oreon_mock_mixed_repos_wrapper_cfg


def test_addrepo_wrapper_disables_enforced_gpg(tmp_path: Path):
    cfg = _write_oreon_mock_mixed_repos_wrapper_cfg(
        "/etc/mock/oreon-11-rp1-x86_64.cfg",
        tmp_path,
        1,
        addrepo_baseurls=["https://repo.example/oreon-11-rp1/side/x86_64/"],
        prefer_arch="x86_64",
    )
    text = cfg.read_text(encoding="utf-8")
    assert "gpgcheck=0" in text
    assert "repo_gpgcheck=0" in text
    assert '_oreon_macros["%_pkgverify_level"] = "none"' in text
    assert "--nogpgcheck" in text
    assert "oreon_addrepo_0_" in text


def test_no_addrepo_skips_gpg_override(tmp_path: Path):
    cfg = _write_oreon_mock_mixed_repos_wrapper_cfg(
        "/etc/mock/oreon-11-rp1-x86_64.cfg",
        tmp_path,
        2,
        addrepo_baseurls=[],
    )
    text = cfg.read_text(encoding="utf-8")
    assert "--nogpgcheck" not in text
    assert "%_pkgverify_level" not in text
