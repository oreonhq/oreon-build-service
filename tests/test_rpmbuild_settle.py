# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from oreon_build.services.rpmbuild_order import names_blocked_by_failures
from oreon_build.services.rpmbuild_settle import (
    _known_sources_from_primary,
    _resolve_primary_key,
    rewrite_distgit_source_url,
)


def test_names_blocked_by_failures_is_transitive() -> None:
    layers = [["lib"], ["mid"], ["app", "other"]]
    blocked = names_blocked_by_failures(
        layers,
        {"lib"},
        {"mid": ["lib"], "app": ["mid"], "other": []},
    )
    assert blocked == {"mid", "app"}


def test_resolve_primary_key_from_repomd() -> None:
    repomd = b"""<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <data type="primary">
    <location href="repodata/primary.xml.gz"/>
  </data>
</repomd>
"""
    key = _resolve_primary_key("oreon/dev/x86_64", repomd)
    assert key == "oreon/dev/x86_64/repodata/primary.xml.gz"


def test_known_sources_from_primary_xml() -> None:
    primary = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://linux.duke.edu/metadata/common" packages="1">
  <package type="rpm">
    <name>foo</name>
    <arch>x86_64</arch>
    <version epoch="0" ver="1.0" rel="1.or1"/>
    <format xmlns:rpm="http://linux.duke.edu/metadata/rpm">
      <rpm:sourcerpm>foo-1.0-1.or1.src.rpm</rpm:sourcerpm>
    </format>
  </package>
</metadata>
"""
    out = _known_sources_from_primary(primary)
    assert "foo" in out


def test_rewrite_distgit_source_url_keeps_spec_path() -> None:
    repo = "https://github.com/oreonhq/rpm-specfiles.git"
    old = f"{repo}#testing/post-beta:oreon-11-rp1/gstreamer1/gstreamer1.spec"
    got = rewrite_distgit_source_url(old, repo, "testing/post-beta")
    assert got == old


def test_rewrite_distgit_source_url_does_not_split_https_scheme() -> None:
    repo = "https://github.com/oreonhq/rpm-specfiles.git"
    old = f"{repo}#testing/post-beta:oreon-11-rp1/gstreamer1/gstreamer1.spec"
    got = rewrite_distgit_source_url(old, repo, "main")
    assert got == f"{repo}#main:oreon-11-rp1/gstreamer1/gstreamer1.spec"


def test_rewrite_distgit_source_url_repairs_already_doubled() -> None:
    repo = "https://github.com/oreonhq/rpm-specfiles.git"
    doubled = (
        f"{repo}#testing/post-beta:"
        f"{repo}#testing/post-beta:oreon-11-rp1/gstreamer1/gstreamer1.spec"
    )
    got = rewrite_distgit_source_url(doubled, repo, "testing/post-beta")
    assert got == f"{repo}#testing/post-beta:oreon-11-rp1/gstreamer1/gstreamer1.spec"
