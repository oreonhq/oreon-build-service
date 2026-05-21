"""Inject must follow BR edges, not whole distgit layers (no kernel/gcc pile-on)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_chain_dep_gate import _make_chain, _make_order


@pytest.mark.asyncio
async def test_inject_does_not_queue_unrelated_layer_neighbors():
    """Earlier topo layers can list unrelated packages; only BR predecessors inject."""
    from oreon_build.services.build_chain import _inject_missing_dep_layers_before

    chain = _make_chain([["target"]], active_stage=0)
    release = MagicMock()
    release.releasename = "oreon-11-rp1"
    order = _make_order(
        edges=[{"from": "target", "to": "dep-a"}],
        layers=[["gcc", "kernel"], ["dep-a"], ["target"]],
        package_names=["gcc", "kernel", "dep-a", "target"],
    )

    db = AsyncMock()
    db.execute.return_value.scalar_one_or_none = MagicMock(return_value=release)

    from oreon_build.services.chain_satisfaction import RepoSatisfactionIndex

    empty_repo = RepoSatisfactionIndex({}, set(), {}, {})

    def _fake_repo_prepare(*_a, **_kw):
        return empty_repo, {"runtime_missing": []}, None, None

    with patch(
        "oreon_build.services.distgit_order.compute_distgit_build_order_for_chain",
        new=AsyncMock(return_value=order),
    ), patch(
        "oreon_build.services.dependencies_graph.load_chain_repo_prepare",
        side_effect=_fake_repo_prepare,
    ), patch(
        "oreon_build.services.build_chain._proactive_extra_roots_from_repo_runtime",
        new=AsyncMock(return_value=(set(), [])),
    ), patch(
        "oreon_build.services.build_chain._batch_chain_packages_still_needing_build",
        new=AsyncMock(side_effect=lambda _db, _chain, names, **_kw: set(names)),
    ), patch(
        "oreon_build.services.build_chain._chain_use_distgit_dep_gate",
        return_value=True,
    ):
        ok = await _inject_missing_dep_layers_before(db, chain, 0)

    assert ok is True
    assert chain.stages_json == [["dep-a"], ["target"]]
    flat = {p for stage in chain.stages_json for p in stage}
    assert "gcc" not in flat
    assert "kernel" not in flat


@pytest.mark.asyncio
async def test_inject_skips_br_providers_already_satisfied_in_repo():
    """Do not queue gcc/kernel when published repo already satisfies those BR lines."""
    from oreon_build.services.build_chain import _inject_missing_dep_layers_before
    from oreon_build.services.chain_satisfaction import RepoSatisfactionIndex, RpmEvr

    chain = _make_chain([["target"]], active_stage=0)
    release = MagicMock()
    release.releasename = "oreon-11-rp1"
    order = _make_order(
        edges=[
            {"from": "target", "to": "gcc"},
            {"from": "target", "to": "kernel"},
            {"from": "target", "to": "dep-a"},
            {"from": "dep-a", "to": "dep-b"},
        ],
        layers=[["gcc", "kernel"], ["dep-b"], ["dep-a"], ["target"]],
        package_names=["gcc", "kernel", "dep-a", "dep-b", "target"],
        build_requires_raw={
            "target": ["gcc", "kernel", "dep-a"],
            "dep-a": ["dep-b"],
        },
    )

    gcc_evr = RpmEvr(0, "14", "1")
    kernel_evr = RpmEvr(0, "6.8", "1")
    repo = RepoSatisfactionIndex(
        provides_index={
            "gcc": {"gcc"},
            "kernel": {"kernel"},
        },
        known_sources={"gcc", "kernel"},
        provide_best_evr={"gcc": gcc_evr, "kernel": kernel_evr},
        source_best_evr={"gcc": gcc_evr, "kernel": kernel_evr},
    )
    order.spec_evrs = {
        "gcc": "0:14-1",
        "kernel": "0:6.8-1",
        "dep-a": "0:1-1",
        "dep-b": "0:1-1",
        "target": "0:1-1",
    }
    order.capability_providers = {
        "gcc": ["gcc"],
        "kernel": ["kernel"],
        "dep-a": ["dep-a"],
        "dep-b": ["dep-b"],
    }

    db = AsyncMock()
    db.execute.return_value.scalar_one_or_none = MagicMock(return_value=release)

    def _fake_repo_prepare(*_a, **_kw):
        return repo, {"runtime_missing": []}, None, None

    with patch(
        "oreon_build.services.distgit_order.compute_distgit_build_order_for_chain",
        new=AsyncMock(return_value=order),
    ), patch(
        "oreon_build.services.dependencies_graph.load_chain_repo_prepare",
        side_effect=_fake_repo_prepare,
    ), patch(
        "oreon_build.services.build_chain._proactive_extra_roots_from_repo_runtime",
        new=AsyncMock(return_value=(set(), [])),
    ), patch(
        "oreon_build.services.build_chain._batch_chain_packages_still_needing_build",
        new=AsyncMock(side_effect=lambda _db, _chain, names, **_kw: set(names)),
    ), patch(
        "oreon_build.services.build_chain._chain_use_distgit_dep_gate",
        return_value=True,
    ):
        ok = await _inject_missing_dep_layers_before(db, chain, 0)

    assert ok is True
    flat = {p for stage in chain.stages_json for p in stage}
    assert "gcc" not in flat
    assert "kernel" not in flat
    assert "dep-a" in flat
    assert "dep-b" in flat
    assert "target" in flat
