from __future__ import annotations

from oreon_build.core.dnf_missing import parse_dnf_missing_provides


def test_parse_requested_and_needed_by():
    text = (
        "nothing provides requested pkgconfig(zlib)\n"
        "Error: nothing provides crate(foo) >= 1 needed by rust-bar.src.rpm\n"
        "\tpkgconfig(bar) >= 1.0 is needed by demo.src.rpm\n"
        "No matching package to install: '/usr/bin/msgfmt'\n"
    )
    got = parse_dnf_missing_provides(text)
    assert "pkgconfig(zlib)" in got
    assert "crate(foo)" in got
    assert "pkgconfig(bar)" in got
    assert "/usr/bin/msgfmt" in got


def test_parse_ignores_noise():
    assert parse_dnf_missing_provides("Transaction failed") == []
