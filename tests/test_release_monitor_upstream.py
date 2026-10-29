# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from oreon_build.services.release_monitor import (
    ingest_anitya_package_items,
    kernel_org_stable_version,
    parse_github_repo,
    _strip_version_tag,
)


def test_strip_version_tag() -> None:
    assert _strip_version_tag("v6.16.8") == "6.16.8"
    assert _strip_version_tag("6.16.8") == "6.16.8"
    assert _strip_version_tag("valgrind-3.24") == "valgrind-3.24"


def test_parse_github_repo() -> None:
    assert parse_github_repo("https://github.com/torvalds/linux.git") == ("torvalds", "linux")
    assert parse_github_repo("https://github.com/oreonhq/foo") == ("oreonhq", "foo")
    assert parse_github_repo("https://example.com/foo") is None


def test_kernel_org_stable_version() -> None:
    payload = {
        "latest_stable": {"version": "6.16.8"},
        "releases": [
            {"version": "6.17", "moniker": "mainline"},
            {"version": "6.16.8", "moniker": "stable"},
        ],
    }
    assert kernel_org_stable_version(payload) == "6.16.8"
    assert kernel_org_stable_version({"releases": [{"version": "6.12.40", "moniker": "stable"}]}) == "6.12.40"


def test_ingest_anitya_package_items() -> None:
    index: dict[str, dict] = {}
    ingest_anitya_package_items(
        index,
        [
            {"name": "bash", "stable_version": "5.3", "project": "bash", "ecosystem": "gnu"},
            {"name": "bash", "version": "5.2"},
            {"name": "", "version": "1"},
        ],
    )
    assert index["bash"]["latest_version"] == "5.3"
