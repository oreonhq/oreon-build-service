# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from oreon_build.core.repo_branch import job_publish_branch
from oreon_build.services.side_tags import (
    PRIMARY_REPO_BRANCHES,
    RESERVED_SIDE_TAG_NAMES,
    can_access_side_tag,
    sanitize_side_tag_name,
    validate_side_tag_name,
)


def test_sanitize_side_tag_name_strips_refs_and_spaces():
    assert sanitize_side_tag_name("refs/heads/feature/foo bar") == "feature/foo-bar"


def test_validate_rejects_primary_and_reserved():
    for name in sorted(PRIMARY_REPO_BRANCHES | RESERVED_SIDE_TAG_NAMES):
        with pytest.raises(HTTPException) as ei:
            validate_side_tag_name(name)
        assert ei.value.status_code == 400


def test_validate_accepts_feature_branch():
    assert validate_side_tag_name("pr-123-fix") == "pr-123-fix"


def test_ci_style_branch_naming():
    raw = "refs/heads/user/my-pr-branch"
    assert validate_side_tag_name(raw) == "user/my-pr-branch"


def test_job_publish_branch_prefers_job_branch():
    rel = SimpleNamespace(default_repo_branch="dev")
    assert job_publish_branch("my-side-tag", rel) == "my-side-tag"
    assert job_publish_branch(None, rel) == "dev"
    assert job_publish_branch("", rel) == "dev"


def test_can_access_side_tag_owner_whitelist_admin():
    owner = SimpleNamespace(id=10, is_admin=False)
    member = SimpleNamespace(id=20, is_admin=False)
    stranger = SimpleNamespace(id=30, is_admin=False)
    admin = SimpleNamespace(id=99, is_admin=True)
    tag = SimpleNamespace(owner_account_id=10, members=[SimpleNamespace(account_id=20)])
    assert can_access_side_tag(owner, tag) is True
    assert can_access_side_tag(member, tag) is True
    assert can_access_side_tag(stranger, tag) is False
    assert can_access_side_tag(admin, tag) is True
    assert can_access_side_tag(None, tag) is False


def test_whole_tree_promote_copies_then_deletes_source_prefix():
    from oreon_build.services.promotion_r2_cleanup import whole_tree_promote_branch

    r2 = MagicMock()
    r2.iter_keys_with_prefix.side_effect = lambda prefix: {
        "rel/from/x86_64/RPMS/": ["rel/from/x86_64/RPMS/pkg-1-1.x86_64.rpm"],
        "rel/from/x86_64/": [],
        "rel/from/src/": [],
        "rel/from/": ["rel/from/x86_64/RPMS/pkg-1-1.x86_64.rpm", "rel/from/repodata/repomd.xml"],
        "rel/from/x86_64/repodata/": [],
    }.get(prefix, [])

    with (
        patch(
            "oreon_build.services.promotion_r2_cleanup.list_branch_basenames",
            return_value=({"pkg-1-1.x86_64.rpm"}, set()),
        ),
        patch(
            "oreon_build.services.promotion_r2_cleanup.copy_branch_to_branch",
            return_value=(["k"], ["pkg-1-1.x86_64.rpm"]),
        ) as copy_mock,
        patch("oreon_build.services.promotion_r2_cleanup.run_batched_promotion_r2_maintenance"),
        patch("oreon_build.services.promotion_r2_cleanup.update_repodata_at_prefix"),
    ):
        result = whole_tree_promote_branch(
            releasename="rel",
            from_branch="from",
            to_branch="dev",
            arches=["x86_64"],
            empty_source=True,
            delete_source_prefix=True,
            r2=r2,
        )
    assert result["copied"] == 1
    assert copy_mock.called
    assert r2.delete_objects.called or r2.delete_object.called


def test_openapi_has_side_tags_and_branch_promote():
    from fastapi.testclient import TestClient
    from oreon_build.api.main import create_app

    client = TestClient(create_app())
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/api/side-tags" in paths
    assert "/api/side-tags/{side_tag_id}/promote" in paths
    assert "/api/promotions/branch" in paths


def test_mock_addrepo_skips_branches_without_repomd():
    from oreon_build.services.repo_solver import mock_addrepo_urls_for_available_branches

    r2 = MagicMock()
    r2.object_exists.side_effect = lambda key: "test-2" in key
    urls = mock_addrepo_urls_for_available_branches(
        "oreon-11-rp1",
        ["test-2"],
        "x86_64",
        r2_client=r2,
    )
    assert urls == ["https://repo-us.oreonhq.com/oreon-11-rp1/test-2/x86_64/"]
    r2.object_exists.side_effect = lambda key: False
    assert mock_addrepo_urls_for_available_branches(
        "oreon-11-rp1", ["test-2"], "x86_64", r2_client=r2
    ) == []
