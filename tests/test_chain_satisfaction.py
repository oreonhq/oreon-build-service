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
