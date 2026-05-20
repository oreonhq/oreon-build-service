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
    chain.chain_spec = ""
    return chain


def _build_requires_raw_from_edges(edges: list[dict[str, str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in edges:
        fr = str(e.get("from") or "").strip()
        to = str(e.get("to") or "").strip()
        if fr and to:
            out.setdefault(fr, []).append(to)
    return out


def _make_order(*, edges, layers, package_names, capability_providers=None, spec_evrs=None, build_requires_raw=None):
    r = MagicMock()
    r.edges = edges
    r.layers = layers
    r.package_names = package_names
    if capability_providers is not None:
        r.capability_providers = capability_providers
    else:
        r.capability_providers = {str(n): [str(n)] for n in package_names if n}
    r.spec_evrs = spec_evrs if spec_evrs is not None else {}
    if build_requires_raw is not None:
        r.build_requires_raw = build_requires_raw
    else:
        r.build_requires_raw = _build_requires_raw_from_edges(edges)
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


def test_format_chain_unresolved_lists_all_capabilities():
    from oreon_build.services.build_chain import format_chain_unresolved_providers_message

    caps = [f"cap-{i}" for i in range(25)]
    msg = format_chain_unresolved_providers_message(
        caps,
        releasename="oreon-11-rp1",
        distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
        distgit_branch="main",
    )
    assert "cap-0" in msg
    assert "cap-24" in msg
    assert "and 10 more" not in msg
    assert "…" not in msg


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
    row = MagicMock()
    row.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=row)
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


def test_requirement_not_satisfied_when_provider_built_but_capability_missing():
    from oreon_build.services.dependencies_graph import _requirement_satisfied_in_repo

    provides_index = {"python3-setuptools-wheel": {"python-setuptools"}}
    assert not _requirement_satisfied_in_repo(
        "python-setuptools-wheel",
        {"python-setuptools"},
        {"python-setuptools"},
        provides_index,
    )
    provides_index["python-setuptools-wheel"] = {"python-setuptools"}
    assert _requirement_satisfied_in_repo(
        "python-setuptools-wheel",
        {"python-setuptools"},
        {"python-setuptools"},
        provides_index,
    )


def test_providers_for_requirement_uses_spec_when_not_in_r2_src():
    from oreon_build.services.dependencies_graph import _providers_for_requirement

    spec_caps = {
        "python-setuptools-wheel": ["python-setuptools"],
        "python-setuptools": ["python-setuptools"],
    }
    got = _providers_for_requirement(
        "python-setuptools-wheel",
        {},
        set(),  # no R2 src/ candidates
        set(),  # nothing built
        spec_caps,
    )
    assert got == {"python-setuptools"}


def test_map_br_to_source_built_subpackage_capability():
    from oreon_build.services.dependencies_graph import _map_br_to_source

    src = {"python-setuptools"}
    known = {"python-setuptools"}
    pi: dict[str, set[str]] = {"python3-setuptools-wheel": {"python-setuptools"}}
    got = _map_br_to_source("python-setuptools-wheel", pi, src, known)
    assert got == {"python-setuptools"}


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
        new=AsyncMock(return_value=set()),
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
        new=AsyncMock(return_value=set()),
    ), patch(
        "oreon_build.services.build_chain._chain_use_distgit_dep_gate",
        return_value=True,
    ):
        ok = await _inject_missing_dep_layers_before(db, chain, 1)

    assert ok is True
    assert chain.stages_json == [["jline"], ["kde-baseapps"], ["kdelibs"]]


def test_chain_user_targets_from_options():
    from datetime import datetime, timezone

    from oreon_build.services.build_chain import (
        _chain_user_target_names,
        _skip_jobs_not_before_for_package,
    )

    chain = _make_chain(
        [["kde-workspace"]],
        chain_options={
            "chain_user_targets": ["kde-workspace"],
            "skip_jobs_not_before": "2026-05-18T12:00:00+00:00",
        },
    )
    assert _chain_user_target_names(chain) == {"kde-workspace"}
    t0 = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    assert _skip_jobs_not_before_for_package(chain, "kde-workspace", t0) == t0
    assert _skip_jobs_not_before_for_package(chain, "kernel", t0) is None


def test_distgit_packages_in_layers_before_skips_when_target_at_layer_zero():
    from oreon_build.services.build_chain import _distgit_packages_in_layers_before

    layers = [["target-pkg"], ["other"]]
    got = _distgit_packages_in_layers_before(
        layers,
        ["target-pkg"],
        user_lower={"target-pkg"},
        already_in_chain_lower=set(),
        spec_names={"target-pkg", "other"},
    )
    assert got == set()


def test_distgit_packages_in_layers_before_user_target():
    from oreon_build.services.build_chain import _distgit_packages_in_layers_before

    layers = [["a", "b"], ["c"], ["target-pkg"]]
    got = _distgit_packages_in_layers_before(
        layers,
        ["target-pkg"],
        user_lower={"target-pkg"},
        already_in_chain_lower=set(),
        spec_names={"a", "b", "c", "target-pkg"},
    )
    assert got == {"a", "b", "c"}


def test_chain_user_targets_parsed_from_spec():
    from oreon_build.services.build_chain import _chain_user_target_names

    chain = _make_chain([["foo", "bar"]])
    chain.chain_spec = "foo bar"
    assert _chain_user_target_names(chain) == {"foo", "bar"}


@pytest.mark.asyncio
async def test_ensure_distgit_source_adds_missing_row():
    from oreon_build.services.build_chain import _ensure_distgit_source_on_package_version

    db = AsyncMock()
    db.flush = AsyncMock()
    scalar = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=scalar))

    await _ensure_distgit_source_on_package_version(
        db,
        99,
        pkg_name="kde-workspace",
        releasename="oreon-11-rp1",
        distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
        distgit_branch="main",
    )
    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert added.kind == "distgit"
    assert added.package_version_id == 99
    assert "oreon-11-rp1/kde-workspace/kde-workspace.spec" in added.url
    db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_ensure_distgit_source_updates_stale_url():
    from oreon_build.services.build_chain import _ensure_distgit_source_on_package_version

    src = MagicMock()
    src.url = "https://github.com/oreonhq/rpm-specfiles.git#main:old/path.spec"
    db = AsyncMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=src)))

    await _ensure_distgit_source_on_package_version(
        db,
        7,
        pkg_name="kde-workspace",
        releasename="oreon-11-rp1",
        distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
        distgit_branch="main",
    )
    db.add.assert_not_called()
    assert "oreon-11-rp1/kde-workspace/kde-workspace.spec" in src.url
    db.flush.assert_awaited()
