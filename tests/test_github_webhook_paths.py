# Oreon Build Service tests — GitHub webhook changed-path resolution

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
