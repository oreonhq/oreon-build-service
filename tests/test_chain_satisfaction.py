"""Repo-aware chain dependency closure."""
from __future__ import annotations

from oreon_build.services.chain_satisfaction import (
    RepoSatisfactionIndex,
    RpmEvr,
    _requirement_status,
    compute_chain_dependency_build_set,
    source_published_at_least_spec,
)
from unittest.mock import MagicMock


def _repo_with_kernel() -> RepoSatisfactionIndex:
    k_evr = RpmEvr(0, "6.8.0", "1.fc43")
    return RepoSatisfactionIndex(
        provides_index={"kernel-devel": {"kernel"}, "kernel": {"kernel"}},
        known_sources={"kernel"},
        provide_best_evr={"kernel-devel": k_evr, "kernel": k_evr},
        source_best_evr={"kernel": k_evr},
    )


def test_repo_satisfied_skips_kernel_inject():
    order = MagicMock()
    order.package_names = ["kde-workspace", "qt6-qtbase", "kernel"]
    order.spec_evrs = {
        "kde-workspace": "0:6.0-1",
        "qt6-qtbase": "0:6.7-1",
        "kernel": "0:6.8.0-1.fc43",
    }
    order.build_requires_raw = {
        "kde-workspace": ["qt6-qtbase-devel"],
        "qt6-qtbase": ["kernel-devel"],
    }
    order.capability_providers = {
        "qt6-qtbase-devel": ["qt6-qtbase"],
        "kernel-devel": ["kernel"],
    }

    repo = _repo_with_kernel()
    plan = compute_chain_dependency_build_set(
        ["kde-workspace"],
        order,
        repo,
        {"kde-workspace"},
    )
    assert "kernel" not in plan.sources_to_build
    assert not plan.conflicts


def test_version_conflict_fails_plan():
    order = MagicMock()
    order.package_names = ["app", "libfoo"]
    order.spec_evrs = {"app": "0:1-1", "libfoo": "0:1-1"}
    order.build_requires_raw = {"app": ["libfoo >= 99.0"]}
    order.capability_providers = {"libfoo": ["libfoo"]}

    repo = RepoSatisfactionIndex(
        provides_index={"libfoo": {"libfoo"}},
        known_sources={"libfoo"},
        provide_best_evr={"libfoo": RpmEvr(0, "1.0", "1")},
        source_best_evr={"libfoo": RpmEvr(0, "1.0", "1")},
    )
    plan = compute_chain_dependency_build_set(["app"], order, repo, {"app"})
    assert plan.conflicts
    assert plan.conflicts[0].consumer == "app"


def test_requirement_missing_not_conflict():
    repo = RepoSatisfactionIndex({}, set(), {}, {})
    status, _ = _requirement_status("not-in-repo", repo)
    assert status == "missing"


def test_source_published_at_least_spec():
    repo = _repo_with_kernel()
    assert source_published_at_least_spec(
        "kernel",
        {"kernel": "0:6.8.0-1.fc43"},
        repo,
    )
    assert not source_published_at_least_spec(
        "kernel",
        {"kernel": "0:6.9.0-1"},
        repo,
    )


def test_chain_sources_missing_from_repo_for_consumers():
    from oreon_build.services.chain_satisfaction import (
        chain_sources_missing_from_repo_for_consumers,
    )

    order = MagicMock()
    order.build_requires_raw = {"kde-workspace": ["kactivities-devel"]}
    order.capability_providers = {"kactivities-devel": ["kactivities"]}
    order.package_names = ["kde-workspace", "kactivities"]

    k_evr = RpmEvr(0, "1.0", "1")
    repo = RepoSatisfactionIndex(
        provides_index={"kactivities": {"kactivities"}},
        known_sources={"kactivities"},
        provide_best_evr={"kactivities": k_evr},
        source_best_evr={"kactivities": k_evr},
    )
    needed = chain_sources_missing_from_repo_for_consumers(
        {"kde-workspace"},
        order,
        repo,
    )
    assert "kactivities" in needed


def test_sources_needing_chain_build_skips_repo_satisfied_predecessors():
    from oreon_build.services.chain_satisfaction import sources_needing_chain_build

    order = MagicMock()
    order.package_names = ["acl", "bash", "kernel-headers"]
    order.spec_evrs = {
        "acl": "0:2.3.1-1.fc43",
        "bash": "0:5.2-1.fc43",
        "kernel-headers": "0:6.8-1.fc43",
    }
    order.build_requires_raw = {"kernel-headers": ["acl"]}
    order.capability_providers = {"acl": ["acl"]}

    acl_evr = RpmEvr(0, "2.3.1", "1.fc43")
    bash_evr = RpmEvr(0, "5.2", "1.fc43")
    repo = RepoSatisfactionIndex(
        provides_index={"acl": {"acl"}, "bash": {"bash"}},
        known_sources={"acl", "bash"},
        provide_best_evr={"acl": acl_evr, "bash": bash_evr},
        source_best_evr={"acl": acl_evr, "bash": bash_evr},
    )
    got = sources_needing_chain_build(
        {"acl", "bash"},
        order,
        repo,
        consumer_packages=["kernel-headers"],
    )
    assert got == set()


def test_chain_source_required_when_devel_br_missing():
    from oreon_build.services.chain_satisfaction import chain_source_still_required_for_consumers

    order = MagicMock()
    order.build_requires_raw = {"kde-workspace": ["kactivities-devel"]}
    order.capability_providers = {"kactivities-devel": ["kactivities"]}
    order.package_names = ["kde-workspace", "kactivities"]

    k_evr = RpmEvr(0, "1.0", "1")
    repo = RepoSatisfactionIndex(
        provides_index={"kactivities": {"kactivities"}},
        known_sources={"kactivities"},
        provide_best_evr={"kactivities": k_evr},
        source_best_evr={"kactivities": k_evr},
    )
    assert chain_source_still_required_for_consumers(
        "kactivities",
        order,
        repo,
        {"kde-workspace"},
    )


def test_devel_br_needs_build_when_only_base_source_in_repo():
    """kactivities in repo must not skip building when kde-workspace still needs kactivities-devel."""
    order = MagicMock()
    order.package_names = ["kde-workspace", "kactivities"]
    order.spec_evrs = {"kde-workspace": "0:1-1", "kactivities": "0:1.0-1"}
    order.build_requires_raw = {"kde-workspace": ["kactivities-devel"]}
    order.capability_providers = {"kactivities-devel": ["kactivities"]}

    k_evr = RpmEvr(0, "1.0", "1")
    repo = RepoSatisfactionIndex(
        provides_index={"kactivities": {"kactivities"}},
        known_sources={"kactivities"},
        provide_best_evr={"kactivities": k_evr},
        source_best_evr={"kactivities": k_evr},
    )
    plan = compute_chain_dependency_build_set(
        ["kde-workspace"],
        order,
        repo,
        {"kde-workspace"},
    )
    assert "kactivities" in plan.sources_to_build
    assert not plan.conflicts
