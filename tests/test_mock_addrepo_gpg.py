from pathlib import Path

from oreon_build.worker.main import _write_oreon_mock_mixed_repos_wrapper_cfg


def test_addrepo_wrapper_pulls_gpgkey_from_mock_dnf_conf(tmp_path: Path):
    cfg = _write_oreon_mock_mixed_repos_wrapper_cfg(
        "/etc/mock/oreon-11-rp1-x86_64.cfg",
        tmp_path,
        1,
        addrepo_baseurls=["https://repo.example/oreon-11-rp1/side/x86_64/"],
        prefer_arch="x86_64",
    )
    text = cfg.read_text(encoding="utf-8")
    assert "mirrors.oreonhq.com" not in text
    assert '_oreon_re.search(r"(?m)^gpgkey\\s*=\\s*(\\S+)", _oreon_dcf)' in text
    assert "_oreon_gpgkey" in text
    assert 'if _oreon_gpgkey:' in text
    assert 'gpgcheck=1\\n' in text or 'gpgcheck=1' in text
    assert 'gpgkey=%s\\n" % (_oreon_gpgkey,)' in text
    assert 'etc/rpm/macros.oreon-sidetag-pkgverify"' in text
    assert "usr/lib/rpm/macros.d/macros.oreon-sidetag-pkgverify" in text
    assert "%_pkgverify_level none" in text
    assert "--nogpgcheck" in text
    assert "oreon_addrepo_0_" in text


def test_addrepo_wrapper_eval_uses_dnf_conf_gpgkey(tmp_path: Path):
    cfg = _write_oreon_mock_mixed_repos_wrapper_cfg(
        "/etc/mock/oreon-11-rp1-x86_64.cfg",
        tmp_path,
        2,
        addrepo_baseurls=["https://repo.example/side/x86_64/"],
    )
    # simulate what mock does after include() filled dnf.conf
    ns: dict = {
        "config_opts": {
            "dnf.conf": (
                "[main]\ngpgcheck=1\n\n"
                "[oreon-testing]\n"
                "name=testing\n"
                "baseurl=https://example/testing/\n"
                "gpgcheck=1\n"
                "gpgkey=https://mirrors.example/RPM-GPG-KEY-oreon\n"
            ),
            "dnf4_common_opts": [],
            "dnf5_common_opts": [],
            "files": {},
        }
    }
    body = "\n".join(
        ln for ln in cfg.read_text(encoding="utf-8").splitlines() if not ln.startswith("include(")
    )
    exec(body, ns, ns)
    dcf = ns["config_opts"]["dnf.conf"]
    assert "gpgkey=https://mirrors.example/RPM-GPG-KEY-oreon" in dcf
    assert "[oreon_addrepo_0_" in dcf
    assert "gpgcheck=1" in dcf


def test_addrepo_wrapper_eval_no_gpgkey_falls_back(tmp_path: Path):
    cfg = _write_oreon_mock_mixed_repos_wrapper_cfg(
        "/etc/mock/oreon-11-rp1-x86_64.cfg",
        tmp_path,
        3,
        addrepo_baseurls=["https://repo.example/side/x86_64/"],
    )
    ns: dict = {
        "config_opts": {
            "dnf.conf": "[main]\nbest=0\n\n[oreon-testing]\nbaseurl=https://example/\n",
            "dnf4_common_opts": [],
            "dnf5_common_opts": [],
            "files": {},
        }
    }
    body = "\n".join(
        ln for ln in cfg.read_text(encoding="utf-8").splitlines() if not ln.startswith("include(")
    )
    exec(body, ns, ns)
    dcf = ns["config_opts"]["dnf.conf"]
    assert "gpgkey=" not in dcf.split("[oreon_addrepo_0_")[1]
    assert "gpgcheck=0" in dcf.split("[oreon_addrepo_0_")[1]


def test_no_addrepo_skips_gpg_override(tmp_path: Path):
    cfg = _write_oreon_mock_mixed_repos_wrapper_cfg(
        "/etc/mock/oreon-11-rp1-x86_64.cfg",
        tmp_path,
        4,
        addrepo_baseurls=[],
    )
    text = cfg.read_text(encoding="utf-8")
    assert "--nogpgcheck" not in text
    assert "macros.oreon-sidetag-pkgverify" not in text
    assert "_oreon_gpgkey" not in text
