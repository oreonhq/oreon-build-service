from __future__ import annotations

from oreon_build.services.build_trigger_bg import uncovered_plan_roots


def test_uncovered_roots_when_only_orphan_deps_planned() -> None:
    """996-style: cascade left rust-* builds but pruned sbctl."""
    assert uncovered_plan_roots(
        ["sbctl"],
        package_plans={"rust-clap_builder": {}, "rust-serde_json": {}},
        skipped=[],
    ) == ["sbctl"]


def test_root_covered_when_build_or_skip() -> None:
    assert uncovered_plan_roots(
        ["sbctl"],
        package_plans={"sbctl": {"build_profile": "full"}},
        skipped=[],
    ) == []
    assert uncovered_plan_roots(
        ["sbctl"],
        package_plans={},
        skipped=["sbctl"],
    ) == []
