# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later


from oreon_build.services.github_push_trigger import (
    GITHUB_PUSH_EMPTY_DIFF_SENTINEL,
    _is_null_github_sha,
    push_touches_distgit_spec,
)


def test_is_null_github_sha() -> None:
    assert _is_null_github_sha("0000000000000000000000000000000000000000")
    assert _is_null_github_sha("0000000000000000000000000000000000000000 ")
    assert not _is_null_github_sha("a" * 40)
    assert _is_null_github_sha(None)
    assert _is_null_github_sha("")


def test_push_touches_empty_means_match_all() -> None:
    assert push_touches_distgit_spec(set(), "oreon-11-rp1/bash/bash.spec")


def test_push_touches_sentinel_never_matches_spec() -> None:
    assert not push_touches_distgit_spec(
        {GITHUB_PUSH_EMPTY_DIFF_SENTINEL},
        "oreon-11-rp1/bash/bash.spec",
    )


def test_push_touches_normal_path() -> None:
    assert push_touches_distgit_spec(
        {"oreon-11-rp1/bash/bash.spec"},
        "oreon-11-rp1/bash/bash.spec",
    )


def test_push_touches_tail_path_without_release_prefix() -> None:
    assert push_touches_distgit_spec(
        {"bash/bash.spec"},
        "oreon-11-rp1/bash/bash.spec",
    )


def test_collect_paths_includes_head_commit_when_commits_empty() -> None:
    from oreon_build.services.github_push_trigger import collect_paths_from_webhook_commits

    body = {
        "commits": [],
        "head_commit": {
            "added": ["oreon-11-rp1/foo/foo.spec"],
            "modified": [],
            "removed": [],
        },
    }
    assert "oreon-11-rp1/foo/foo.spec" in collect_paths_from_webhook_commits(body)


def test_collect_paths_excludes_removed_files() -> None:
    from oreon_build.services.github_push_trigger import (
        collect_paths_from_compare_files,
        collect_paths_from_webhook_commits,
    )

    body = {
        "commits": [
            {
                "added": [],
                "modified": ["oreon-11-rp1/bash/bash.spec"],
                "removed": ["oreon-11-rp1/kf5-kirigami/kf5-kirigami.spec"],
            }
        ],
        "head_commit": {
            "added": [],
            "modified": [],
            "removed": ["oreon-11-rp1/kf5-purpose/kf5-purpose.spec"],
        },
    }
    paths = collect_paths_from_webhook_commits(body)
    assert "oreon-11-rp1/bash/bash.spec" in paths
    assert "oreon-11-rp1/kf5-kirigami/kf5-kirigami.spec" not in paths
    assert "oreon-11-rp1/kf5-purpose/kf5-purpose.spec" not in paths

    compare = {
        "files": [
            {"filename": "oreon-11-rp1/bash/bash.spec", "status": "modified"},
            {"filename": "oreon-11-rp1/kf5-kirigami/kf5-kirigami.spec", "status": "removed"},
            {
                "filename": "oreon-11-rp1/vim/vim.spec",
                "previous_filename": "oreon-11-rp1/vim-old/vim-old.spec",
                "status": "renamed",
            },
        ]
    }
    cpaths = collect_paths_from_compare_files(compare)
    assert "oreon-11-rp1/bash/bash.spec" in cpaths
    assert "oreon-11-rp1/kf5-kirigami/kf5-kirigami.spec" not in cpaths
    assert "oreon-11-rp1/vim/vim.spec" in cpaths
    assert "oreon-11-rp1/vim-old/vim-old.spec" in cpaths


def test_push_webhook_ref_advanced() -> None:
    from oreon_build.services.github_push_trigger import push_webhook_ref_advanced

    assert push_webhook_ref_advanced(
        {"before": "a" * 40, "after": "b" * 40},
    )
    assert push_webhook_ref_advanced(
        {"before": "0" * 40, "after": "b" * 40},
    )
    assert not push_webhook_ref_advanced(
        {"before": "a" * 40, "after": "a" * 40},
    )
