# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

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


def test_parse_cmake_virtual_from_mock_installdeps():
    text = (
        "No matching package to install: 'cmake(KQuickImageEditor)'\n"
        "No matching package to install: 'cmake(OpenCV)'\n"
        "Not all dependencies satisfied\n"
    )
    got = parse_dnf_missing_provides(text)
    assert "cmake(KQuickImageEditor)" in got
    assert "cmake(OpenCV)" in got


def test_parse_ignores_noise():
    assert parse_dnf_missing_provides("Transaction failed") == []


def test_malformed_buildrequires_token():
    from oreon_build.core.rpm_requires import is_malformed_rpm_requirement

    assert is_malformed_rpm_requirement("BuildRequires:")
    assert is_malformed_rpm_requirement("requested BuildRequires:")
    assert is_malformed_rpm_requirement("Requires")
    assert not is_malformed_rpm_requirement("cmake")
    assert not is_malformed_rpm_requirement("pkgconfig(foo)")
    from oreon_build.core.dnf_missing import parse_dnf_missing_provides

    got = parse_dnf_missing_provides("No matching package to install: 'BuildRequires:'\n")
    assert got
    assert all(is_malformed_rpm_requirement(c) for c in got)
