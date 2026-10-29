# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from oreon_build.worker.main import _parse_spectool_list_entry


def test_parse_spectool_source_and_patch_lines() -> None:
    assert _parse_spectool_list_entry("Source0: iptables-1.8.11.tar.xz") == "iptables-1.8.11.tar.xz"
    assert (
        _parse_spectool_list_entry("Source1 (sha256): https://example.com/foo.tar.gz")
        == "https://example.com/foo.tar.gz"
    )
    assert _parse_spectool_list_entry("Patch3: fix-something.patch") == "fix-something.patch"


def test_parse_spectool_ignores_gpg_noise() -> None:
    assert _parse_spectool_list_entry("gpg: keybox '/tmp/pubring.kbx' created") is None
    assert (
        _parse_spectool_list_entry(
            "gpg: key D70D1A666ACF2B21: 1 signature not checked due to a missing key"
        )
        is None
    )
    assert _parse_spectool_list_entry("gpg: /tmp/trustdb.gpg: trustdb created") is None
