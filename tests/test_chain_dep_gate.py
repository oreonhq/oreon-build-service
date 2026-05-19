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


def _make_order(*, edges, layers, package_names, capability_providers=None):
    r = MagicMock()
    r.edges = edges
    r.layers = layers
    r.package_names = package_names
    r.capability_providers = capability_providers if capability_providers is not None else {}
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


def test_format_chain_unresolved_providers_message():
    from oreon_build.services.build_chain import format_chain_unresolved_providers_message

    msg = format_chain_unresolved_providers_message(
        ["libfoo-missing-cap"],
        releasename="oreon-11-rp1",
        distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
        distgit_branch="main",
        context="Build of foo failed in mock.",
    )
    assert "Chain build stopped" in msg
    assert "libfoo-missing-cap" in msg
    assert "oreon-11-rp1" in msg
    assert "lookaside" in msg.lower()


@pytest.mark.asyncio
async def test_unresolved_chain_capabilities():
    from oreon_build.services.build_chain import unresolved_chain_capabilities

    order = _make_order(
        edges=[],
        layers=[],
        package_names=["libfoo"],
        capability_providers={"libfoo-plugin": ["libfoo"]},
    )
    db = AsyncMock()
    assert await unresolved_chain_capabilities(db, order, ["libfoo-plugin"]) == []
    assert await unresolved_chain_capabilities(db, order, ["not-in-tree"]) == ["not-in-tree"]


def test_resolve_capability_via_spec_provide_only():
    from oreon_build.services.distgit_order import resolve_distgit_sources_for_capability

    order = _make_order(
        edges=[],
        layers=[],
        package_names=["libfoo"],
        capability_providers={"libfoo-plugin": ["libfoo"]},
    )
    assert resolve_distgit_sources_for_capability(order, "libfoo-plugin") == ["libfoo"]
    assert resolve_distgit_sources_for_capability(order, "libfoo-plugin-wheel") == []


def test_parse_versioned_provides_capability_name():
    from oreon_build.services.distgit_order import _parse_spec_text, _provider_map_from_specs

    spec = """
Name: libfoo
%package wheel
Provides: libfoo-missing-cap = 1.0-1
"""
    parsed = _parse_spec_text(spec, "libfoo.spec")
    assert "libfoo-missing-cap" in parsed.provided_binary_names
    pm = _provider_map_from_specs({"libfoo": parsed})
    assert "libfoo" in pm.get("libfoo-missing-cap", [])


def test_sources_missing_repo_capabilities():
    from oreon_build.services.build_chain import _sources_missing_repo_capabilities

    order = _make_order(
        edges=[],
        layers=[],
        package_names=["libfoo"],
        capability_providers={"libfoo-missing-cap": ["libfoo"]},
    )
    repo_pi: dict[str, set[str]] = {}
    got = _sources_missing_repo_capabilities(order, repo_pi, ["libfoo-missing-cap"])
    assert got == {"libfoo"}


def test_parse_spec_macro_expanded_subpackage_is_capability():
    from oreon_build.services.distgit_order import _parse_spec_text

    spec = """
Name: python-libfoo
%global srcname libfoo
%global wheel_prefix python3
%package -n %{wheel_prefix}-%{srcname}-plugin
Provides: libfoo-missing-cap
"""
    parsed = _parse_spec_text(spec, "python-libfoo.spec")
    assert "python3-libfoo-plugin" in parsed.provided_binary_names
    assert "libfoo-missing-cap" in parsed.provided_binary_names


def test_parse_dnf_missing_providers():
    from oreon_build.services.build_chain import parse_dnf_missing_providers

    text = (
        "Problem: conflicting requests\n"
        "- nothing provides libfoo-missing-cap needed by "
        "libbar-1.0-1.fc43.x86_64 from oreon-base\n"
    )
    assert parse_dnf_missing_providers(text) == ["libfoo-missing-cap"]


def test_resolve_distgit_sources_for_missing_providers():
    from oreon_build.services.build_chain import resolve_distgit_sources_for_missing_providers

    order = _make_order(
        edges=[],
        layers=[["libfoo"], ["libbar"]],
        package_names=["libfoo", "libbar"],
        capability_providers={"libfoo-plugin": ["libfoo"]},
    )
    got = resolve_distgit_sources_for_missing_providers(
        order,
        ["libfoo-plugin"],
    )
    assert got == {"libfoo"}


@pytest.mark.asyncio
async def test_resolve_chain_sources_falls_back_to_package_registry():
    from oreon_build.services.build_chain import resolve_chain_sources_for_missing_providers

    order = _make_order(
        edges=[],
        layers=[],
        package_names=[],
        capability_providers={},
    )
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value="libfoo"))
    )
    got = await resolve_chain_sources_for_missing_providers(
        db,
        order,
        ["libfoo-plugin"],
    )
    assert got == {"libfoo"}


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
async def test_inject_does_not_duplicate_packages_already_on_chain():
    from oreon_build.services.build_chain import _inject_missing_dep_layers_before

    chain = _make_chain([["libbar"], ["firefox"]], active_stage=0)
    release = MagicMock()
    release.releasename = "oreon-11-rp1"
    order = _make_order(
        edges=[{"from": "firefox", "to": "libbar"}],
        layers=[["libbar"], ["firefox"]],
        package_names=["libbar", "firefox"],
    )

    db = AsyncMock()
    db.execute.return_value.scalar_one_or_none = MagicMock(return_value=release)

    with patch(
        "oreon_build.services.build_chain._cached_distgit_build_order",
        new=AsyncMock(return_value=order),
    ), patch(
        "oreon_build.services.build_chain._cached_repo_provides_index",
        new=AsyncMock(return_value=({}, set())),
    ), patch(
        "oreon_build.services.build_chain._proactive_extra_roots_from_repo_runtime",
        new=AsyncMock(return_value=(set(), [])),
    ), patch(
        "oreon_build.services.build_chain._package_id_for_name",
        new=AsyncMock(return_value=99),
    ), patch(
        "oreon_build.services.build_chain._chain_package_still_needs_build",
        new=AsyncMock(return_value=True),
    ), patch(
        "oreon_build.services.build_chain._chain_use_distgit_dep_gate",
        return_value=True,
    ):
        ok = await _inject_missing_dep_layers_before(db, chain, 0)

    assert ok is True
    assert chain.stages_json == [["libbar"], ["firefox"]]


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
