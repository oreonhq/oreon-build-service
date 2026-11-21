# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from oreon_build.services.promotion_r2_cleanup import (
    binary_source_names,
    dropped_sibling_filenames,
    parse_primary_sourcerpms,
    promote_filenames_without_stale_siblings,
    source_names_from_filenames,
)

PRIMARY = b"""<?xml version="1.0"?>
<metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm">
  <package type="rpm">
    <name>foo-libs</name>
    <arch>x86_64</arch>
    <version epoch="0" ver="1.0" rel="1.or11"/>
    <checksum type="sha256" pkgid="YES">aa</checksum>
    <location href="RPMS/foo-libs-1.0-1.or11.x86_64.rpm"/>
    <format>
      <rpm:sourcerpm>foo-1.0-1.or11.src.rpm</rpm:sourcerpm>
    </format>
  </package>
  <package type="rpm">
    <name>foo</name>
    <arch>x86_64</arch>
    <version epoch="0" ver="1.0" rel="1.or11"/>
    <location href="RPMS/foo-1.0-1.or11.x86_64.rpm"/>
    <format>
      <rpm:sourcerpm>foo-1.0-1.or11.src.rpm</rpm:sourcerpm>
    </format>
  </package>
  <package type="rpm">
    <name>bar-libs</name>
    <arch>x86_64</arch>
    <version epoch="0" ver="2" rel="1"/>
    <location href="RPMS/bar-libs-2-1.x86_64.rpm"/>
    <format>
      <rpm:sourcerpm>bar-2-1.src.rpm</rpm:sourcerpm>
    </format>
  </package>
</metadata>
"""


def test_parse_primary_records_sourcerpm():
    got = parse_primary_sourcerpms(PRIMARY)
    assert got["foo-libs-1.0-1.or11.x86_64.rpm"] == "foo-1.0-1.or11.src.rpm"
    assert got["bar-libs-2-1.x86_64.rpm"] == "bar-2-1.src.rpm"


def test_dropped_sibling_is_removed_and_unrelated_package_stays():
    mapping = binary_source_names(parse_primary_sourcerpms(PRIMARY))
    keep = {
        "foo-1.2-1.or11.x86_64.rpm",
        "foo-devel-1.2-1.or11.x86_64.rpm",
        "foo-1.2-1.or11.src.rpm",
    }
    drop = set(dropped_sibling_filenames(mapping, source_names_from_filenames(keep), keep))
    assert "foo-libs-1.0-1.or11.x86_64.rpm" in drop
    assert "foo-1.0-1.or11.x86_64.rpm" in drop
    assert "bar-libs-2-1.x86_64.rpm" not in drop


def test_promote_skips_stale_sibling_still_sitting_in_the_side_tag():
    mapping = parse_primary_sourcerpms(PRIMARY)
    filenames = [
        "foo-1.2-1.or11.src.rpm",
        "foo-1.2-1.or11.x86_64.rpm",
        "foo-libs-1.0-1.or11.x86_64.rpm",
        "bar-libs-2-1.x86_64.rpm",
    ]
    kept = promote_filenames_without_stale_siblings(filenames, mapping)
    assert "foo-libs-1.0-1.or11.x86_64.rpm" not in kept
    assert "foo-1.2-1.or11.x86_64.rpm" in kept
    assert "foo-1.2-1.or11.src.rpm" in kept
    assert "bar-libs-2-1.x86_64.rpm" in kept
