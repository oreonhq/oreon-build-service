"""Tests for the chain dep gate: chain-internal packages must never block stage advancement."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_chain(stages_json, active_stage=0, chain_options=None):
    chain = MagicMock()
    chain.stages_json = stages_json
    chain.active_stage_index = active_stage
    chain.release_id = 1
    chain.id = 42
    chain.chain_options = chain_options or {
        "architectures": ["x86_64"],
        "distgit_url": "https://github.com/oreonhq/rpm-specfiles.git",
        "distgit_branch": "main",
        "preferred_mock_environment_id": None,
    }
    return chain


def _make_order_result(edges):
    r = MagicMock()
    r.edges = edges
    return r


@pytest.mark.asyncio
async def test_dep_gate_skips_chain_internal_deps():
    """
    kde-baseapps depends on kdelibs per distgit edges.
    kdelibs is in the same chain (later stage).
    The gate must NOT block advancement to stage 1.
    """
    from oreon_build.services.build_chain import _chain_distgit_predecessors_built_for_stage

    stages = [
        ["jline"],
        ["kde-baseapps"],
        ["kde-workspace"],
        ["kdelibs"],
    ]
    chain = _make_chain(stages, active_stage=0)

    db = AsyncMock()
    release = MagicMock()
    release.releasename = "oreon-11-rp1"
    db.execute.return_value.scalar_one_or_none = MagicMock(return_value=release)

    edges = [
        {"from": "kde-baseapps", "to": "kdelibs"},
        {"from": "kde-workspace", "to": "kdelibs"},
    ]
    order_result = _make_order_result(edges)

    with patch(
        "oreon_build.services.build_chain._cached_distgit_build_order",
        new=AsyncMock(return_value=order_result),
    ), patch(
        "oreon_build.services.build_chain.get_or_create_build_target",
        new=AsyncMock(return_value=MagicMock(id=1)),
    ), patch(
        "oreon_build.services.build_chain.has_successful_build_for_target",
        new=AsyncMock(return_value=False),
    ):
        result = await _chain_distgit_predecessors_built_for_stage(db, chain, next_stage_index=1)

    assert result is True, (
        "Gate must pass when the predecessor (kdelibs) is itself in the chain. "
        "Blocking on it causes a deadlock."
    )


@pytest.mark.asyncio
async def test_dep_gate_blocks_on_external_unbuilt_dep():
    """External dep (not in chain) that hasn't been built should still block."""
    from oreon_build.services.build_chain import _chain_distgit_predecessors_built_for_stage

    stages = [["jline"], ["my-app"]]
    chain = _make_chain(stages, active_stage=0)

    db = AsyncMock()
    release = MagicMock()
    release.releasename = "oreon-11-rp1"
    db.execute.return_value.scalar_one_or_none = MagicMock(return_value=release)

    edges = [{"from": "my-app", "to": "some-external-lib"}]
    order_result = _make_order_result(edges)

    pkg_row = MagicMock()
    pkg_row.scalar_one_or_none = MagicMock(return_value=99)

    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one_or_none=MagicMock(return_value=release)),
        pkg_row,
    ])

    with patch(
        "oreon_build.services.build_chain._cached_distgit_build_order",
        new=AsyncMock(return_value=order_result),
    ), patch(
        "oreon_build.services.build_chain.get_or_create_build_target",
        new=AsyncMock(return_value=MagicMock(id=1)),
    ), patch(
        "oreon_build.services.build_chain.has_successful_build_for_target",
        new=AsyncMock(return_value=False),
    ):
        result = await _chain_distgit_predecessors_built_for_stage(db, chain, next_stage_index=1)

    assert result is False, "External unbuilt dep should still block advancement"


@pytest.mark.asyncio
async def test_dep_gate_passes_when_external_dep_already_built():
    """External dep that's already built should not block."""
    from oreon_build.services.build_chain import _chain_distgit_predecessors_built_for_stage

    stages = [["jline"], ["my-app"]]
    chain = _make_chain(stages, active_stage=0)

    db = AsyncMock()
    release = MagicMock()
    release.releasename = "oreon-11-rp1"
    db.execute.return_value.scalar_one_or_none = MagicMock(return_value=release)

    edges = [{"from": "my-app", "to": "some-external-lib"}]
    order_result = _make_order_result(edges)

    pkg_row = MagicMock()
    pkg_row.scalar_one_or_none = MagicMock(return_value=99)

    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one_or_none=MagicMock(return_value=release)),
        pkg_row,
    ])

    with patch(
        "oreon_build.services.build_chain._cached_distgit_build_order",
        new=AsyncMock(return_value=order_result),
    ), patch(
        "oreon_build.services.build_chain.get_or_create_build_target",
        new=AsyncMock(return_value=MagicMock(id=1)),
    ), patch(
        "oreon_build.services.build_chain.has_successful_build_for_target",
        new=AsyncMock(return_value=True),
    ):
        result = await _chain_distgit_predecessors_built_for_stage(db, chain, next_stage_index=1)

    assert result is True, "External dep that is already built should not block"
