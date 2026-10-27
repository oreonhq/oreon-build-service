from oreon_build.api.routers.auth import _login_hits, login_rate_limited
from oreon_build.services.rpm_scan import parse_clam_line, parse_yr_json, skip_scan_path, _scan_failure
from pathlib import Path


def test_skip_noise_paths():
    assert skip_scan_path("usr/share/doc/foo/README")
    assert skip_scan_path("usr/lib/tests/payload")
    assert skip_scan_path("opt/pkg/testdata/sample")
    assert skip_scan_path("usr/share/rules/a.yar")
    assert not skip_scan_path("usr/bin/curl")


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


def test_login_rate_limit_trips():
    _login_hits.clear()
    ip = "203.0.113.9"
    assert all(login_rate_limited(ip, now=1000 + i) is False for i in range(12))
    assert login_rate_limited(ip, now=1012) is True
    _login_hits.clear()
