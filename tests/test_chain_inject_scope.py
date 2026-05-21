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
