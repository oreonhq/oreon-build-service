"""Chain auto-injection of missing distgit dependencies."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_chain(stages_json, active_stage=0, chain_options=None):
    chain = MagicMock()
    chain.stages_json = stages_json
    chain.active_stage_index = active_stage
    chain.release_id = 1
    chain.id = 42
    chain.status = "running"
    chain.chain_options = chain_options or {
        "architectures": ["x86_64"],
        "distgit_url": "https://github.com/oreonhq/rpm-specfiles.git",
        "distgit_branch": "main",
        "preferred_mock_environment_id": None,
        "skip_complete_arches": True,
    }
    return chain


def _make_order(*, edges, layers, package_names):
    r = MagicMock()
    r.edges = edges
    r.layers = layers
    r.package_names = package_names
    return r


def test_transitive_spec_predecessors():
    from oreon_build.services.build_chain import _transitive_spec_predecessors

    deps = {
        "firefox": {"libfoo"},
        "libfoo": {"libbar"},
    }
    spec = {"firefox", "libfoo", "libbar"}
    got = _transitive_spec_predecessors(deps, ["firefox"], spec)
    assert got == {"libfoo", "libbar"}


def test_layers_for_package_subset_preserves_order():
    from oreon_build.services.build_chain import _layers_for_package_subset

    layers = [["libbar"], ["libfoo"], ["firefox"]]
    got = _layers_for_package_subset({"libfoo", "libbar"}, layers)
    assert got == [["libbar"], ["libfoo"]]


@pytest.mark.asyncio
async def test_inject_before_stage_zero_for_single_package_chain():
    from oreon_build.services.build_chain import _inject_missing_dep_layers_before

    chain = _make_chain([["firefox"]], active_stage=0)
    release = MagicMock()
    release.releasename = "oreon-11-rp1"

    order = _make_order(
        edges=[
            {"from": "firefox", "to": "libfoo"},
            {"from": "libfoo", "to": "libbar"},
        ],
        layers=[["libbar"], ["libfoo"], ["firefox"]],
        package_names=["libbar", "libfoo", "firefox"],
    )

    db = AsyncMock()
    db.execute.return_value.scalar_one_or_none = MagicMock(return_value=release)

    with patch(
        "oreon_build.services.build_chain._cached_distgit_build_order",
        new=AsyncMock(return_value=order),
    ), patch(
        "oreon_build.services.build_chain._package_id_for_name",
        new=AsyncMock(return_value=None),
    ), patch(
        "oreon_build.services.build_chain._chain_use_distgit_dep_gate",
        return_value=True,
    ):
        ok = await _inject_missing_dep_layers_before(db, chain, 0)

    assert ok is True
    assert chain.stages_json == [["libbar"], ["libfoo"], ["firefox"]]


@pytest.mark.asyncio
async def test_inject_skips_chain_internal_packages():
    from oreon_build.services.build_chain import _inject_missing_dep_layers_before

    chain = _make_chain(
        [["jline"], ["kde-baseapps"], ["kdelibs"]],
        active_stage=0,
    )
    release = MagicMock()
    release.releasename = "oreon-11-rp1"
    order = _make_order(
        edges=[{"from": "kde-baseapps", "to": "kdelibs"}],
        layers=[["jline"], ["kde-baseapps"], ["kdelibs"]],
        package_names=["jline", "kde-baseapps", "kdelibs"],
    )

    db = AsyncMock()
    db.execute.return_value.scalar_one_or_none = MagicMock(return_value=release)

    with patch(
        "oreon_build.services.build_chain._cached_distgit_build_order",
        new=AsyncMock(return_value=order),
    ), patch(
        "oreon_build.services.build_chain._package_id_for_name",
        new=AsyncMock(return_value=99),
    ), patch(
        "oreon_build.services.build_chain._chain_has_successful_build_all_arches",
        new=AsyncMock(return_value=False),
    ):
        ok = await _inject_missing_dep_layers_before(db, chain, 1)

    assert ok is True
    assert chain.stages_json == [["jline"], ["kde-baseapps"], ["kdelibs"]]
