from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oreon_build.models import (
    BuildStatus,
    BuildTarget,
    LayerJobBlocker,
    LayerPlan,
    LayerPlanStatus,
    Package,
    Release,
)
from oreon_build.services.distgit_order import _SpecParseResult
from oreon_build.services.layer_engine import (
    _capability_arches_for_job,
    _cheap_unblock_if_blockers_done,
    _filter_build_set,
    _recheck_job_ready,
    _revive_upstream_cancelled_dependents,
    _spawn_layer_dep_sources,
    _static_close_dependencies,
    break_dep_cycles,
    cascade_fail_dependents,
    deadlock_blocker_cycles,
    finalize_blocked_on_dead_upstreams,
    initial_job_status,
    on_package_published,
    parse_missing_provides_from_text,
    prune_unneeded_layer_sources,
    resolve_layer_blocker_cycles,
    try_autofix_layer_missing_provides,
)
from oreon_build.services.layer_plan import on_build_job_failed_for_layer
from oreon_build.services.repo_capability_index import RepoCapabilityIndex


def _index_with(*, provides: set[str] = frozenset(), source_names: set[str] = frozenset()) -> RepoCapabilityIndex:
    idx = RepoCapabilityIndex()
    idx.provides |= set(provides)
    idx.binary_names |= set(provides)
    idx.source_names |= set(source_names)
    idx.finalize_lookups()
    return idx


def _ok_solver() -> MagicMock:
    sol = MagicMock()
    sol.resolve.return_value = MagicMock(ok=True, missing_provides=set())
    return sol


def _close_kwargs() -> dict[str, Any]:
    return {
        "releasename": "oreon-11",
        "owner": "oreonhq",
        "repo": "rpm-specfiles",
        "distgit_branch": "main",
        "headers": {},
        "target_arches": None,
        "publish_branch": "test-side",
        "architectures": ["x86_64"],
    }


def test_initial_job_status_queues_when_no_blockers():
    status, reason = initial_job_status(set())
    assert status == BuildStatus.QUEUED
    assert reason is None


def test_initial_job_status_blocks_and_lists_sources():
    status, reason = initial_job_status({"libfoo", "libbar"})
    assert status == BuildStatus.BLOCKED
    assert reason is not None
    assert "libbar" in reason and "libfoo" in reason


def test_initial_job_status_truncates_long_blocker_list():
    blockers = {f"pkg{i}" for i in range(20)}
    status, reason = initial_job_status(blockers)
    assert status == BuildStatus.BLOCKED
    # only the first 8 (sorted) show up in the human-readable reason
    assert reason.count(",") <= 7


@pytest.mark.asyncio
async def test_static_close_dependencies_skips_already_satisfied_reqs():
    """A BuildRequires already provided by the real repo is not a blocking edge."""
    parsed = {
        "leaf": _SpecParseResult(
            source_name="leaf",
            build_requires=["pkg-config"],
            provided_binary_names={"leaf"},
            build_requires_raw=["pkg-config"],
        ),
    }
    cap_index = _index_with(provides={"pkg-config"}, source_names={"leaf"})

    with patch(
        "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
        new=AsyncMock(return_value=None),
    ), patch(
        "oreon_build.services.layer_engine.expand_chain_spec_closure_sparse",
        new=AsyncMock(return_value=({"leaf": "spec text"}, parsed, [])),
    ), patch(
        "oreon_build.services.layer_engine.get_repo_solver",
        return_value=_ok_solver(),
    ):
        deps, known, errors, unresolved = await _static_close_dependencies(
            ["leaf"],
            cap_index=cap_index,
            **_close_kwargs(),
        )

    assert known == {"leaf"}
    assert deps["leaf"] == set()
    assert errors == []
    assert unresolved["leaf"] == set()


@pytest.mark.asyncio
async def test_static_close_dependencies_records_unbuilt_distgit_provider_as_blocker():
    """A BuildRequires satisfied only by another (unbuilt) distgit source blocks the consumer."""
    parsed = {
        "top": _SpecParseResult(
            source_name="top",
            build_requires=["libfoo-devel"],
            provided_binary_names={"top"},
            build_requires_raw=["libfoo-devel"],
        ),
        "libfoo": _SpecParseResult(
            source_name="libfoo",
            build_requires=[],
            provided_binary_names={"libfoo-devel"},
            build_requires_raw=[],
        ),
    }
    cap_index = _index_with(source_names={"top", "libfoo"})

    with patch(
        "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
        new=AsyncMock(return_value=None),
    ), patch(
        "oreon_build.services.layer_engine.expand_chain_spec_closure_sparse",
        new=AsyncMock(
            return_value=({"top": "spec top", "libfoo": "spec libfoo"}, parsed, [])
        ),
    ), patch(
        "oreon_build.services.layer_engine.providers_for_requirement",
        return_value={"libfoo"},
    ), patch(
        "oreon_build.services.layer_engine.get_repo_solver",
        return_value=_ok_solver(),
    ):
        deps, known, errors, unresolved = await _static_close_dependencies(
            ["top"],
            cap_index=cap_index,
            **_close_kwargs(),
        )

    assert deps["top"] == {"libfoo"}
    assert deps["libfoo"] == set()
    assert known == {"top", "libfoo"}
    assert errors == []
    assert unresolved["top"] == set()


def test_parse_missing_provides_from_mock_log():
    text = """
Error:
Problem 1: nothing provides requested (crate(askalono/default) >= 0.5.0 with crate(askalono/default) < 0.6.0~)
Problem 2: nothing provides requested (crate(rayon/default) >= 1.10.0 with crate(rayon/default) < 2.0.0~)
"""
    got = parse_missing_provides_from_text(text)
    assert any("askalono" in c for c in got)
    assert any("rayon" in c for c in got)


def test_parse_missing_provides_no_matching_package_to_install():
    text = """
No matches found for the following disable plugin patterns: local, spacewalk, versionlock
Last metadata expiration check: 0:00:28 ago on Sat Aug  8 18:02:43 2026.
No matching package to install: 'python3-matplotlib'
Not all dependencies satisfied
Error: Some packages could not be found.
"""
    got = parse_missing_provides_from_text(text)
    assert got == set()


@pytest.mark.asyncio
async def test_static_close_skips_evr_current_transitive_provider():
    """Repo already has provider at >= distgit EVR: do not enqueue rebuild."""
    parsed = {
        "top": _SpecParseResult(
            source_name="top",
            build_requires=["crate(clap)"],
            provided_binary_names={"top"},
            build_requires_raw=["crate(clap)"],
            version="1.0.0",
            release="1",
        ),
        "rust-clap": _SpecParseResult(
            source_name="rust-clap",
            build_requires=[],
            provided_binary_names={"rust-clap", "crate(clap)"},
            build_requires_raw=[],
            version="4.5.0",
            release="1.or11",
        ),
    }
    cap_index = RepoCapabilityIndex(
        source_names={"rust-clap"},
        binary_names={"rust-clap"},
        provides={"rust-clap"},
        capability_providers={"rust-clap": {"rust-clap"}},
        capability_evrs={"rust-clap": [("0", "4.5.0", "1.or11")]},
        binary_source={"rust-clap": "rust-clap"},
    )
    cap_index.finalize_lookups()

    with patch(
        "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
        new=AsyncMock(return_value=None),
    ), patch(
        "oreon_build.services.layer_engine.expand_chain_spec_closure_sparse",
        new=AsyncMock(return_value=({"top": "t", "rust-clap": "c"}, parsed, [])),
    ), patch(
        "oreon_build.services.layer_engine.providers_for_requirement",
        return_value={"rust-clap"},
    ), patch(
        "oreon_build.services.layer_engine.get_repo_solver",
        return_value=_ok_solver(),
    ):
        deps, known, errors, unresolved = await _static_close_dependencies(
            ["top"],
            cap_index=cap_index,
            **_close_kwargs(),
        )

    assert "rust-clap" in known
    assert known == {"top", "rust-clap"}
    assert deps.get("top", set()) == {"rust-clap"}
    assert errors == []


@pytest.mark.asyncio
async def test_static_close_skips_runtime_requires_already_in_repo():
    """Runtime Requires that repo already satisfies do not expand rebuilds."""
    parsed = {
        "or11-backgrounds": _SpecParseResult(
            source_name="or11-backgrounds",
            build_requires=["kde-filesystem", "make"],
            provided_binary_names={"or11-backgrounds"},
            build_requires_raw=["kde-filesystem", "make"],
            requires=["oreon-wallpapers", "xfdesktop"],
            requires_raw=["oreon-wallpapers", "xfdesktop"],
        ),
        "xfdesktop": _SpecParseResult(
            source_name="xfdesktop",
            build_requires=["libxfce4ui-devel"],
            provided_binary_names={"xfdesktop"},
            build_requires_raw=["libxfce4ui-devel"],
        ),
    }
    cap_index = _index_with(
        provides={"kde-filesystem", "make", "oreon-wallpapers", "xfdesktop"},
        source_names={"or11-backgrounds", "oreon-wallpapers", "xfdesktop"},
    )

    with patch(
        "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
        new=AsyncMock(return_value=None),
    ), patch(
        "oreon_build.services.layer_engine.expand_chain_spec_closure_sparse",
        new=AsyncMock(
            return_value=(
                {"or11-backgrounds": "spec"},
                {"or11-backgrounds": parsed["or11-backgrounds"]},
                [],
            )
        ),
    ), patch(
        "oreon_build.services.layer_engine.get_repo_solver",
        return_value=_ok_solver(),
    ):
        deps, known, errors, unresolved = await _static_close_dependencies(
            ["or11-backgrounds"],
            cap_index=cap_index,
            **_close_kwargs(),
        )

    assert known == {"or11-backgrounds"}
    assert "xfdesktop" not in known
    assert deps.get("or11-backgrounds", set()) == set()
    assert errors == []


@pytest.mark.asyncio
async def test_static_close_does_not_expand_missing_runtime_requires():
    """Runtime Requires do not pull new packages into the build set."""
    parsed = {
        "or11-backgrounds": _SpecParseResult(
            source_name="or11-backgrounds",
            build_requires=["make"],
            provided_binary_names={"or11-backgrounds"},
            build_requires_raw=["make"],
            requires=["xfdesktop"],
            requires_raw=["xfdesktop"],
        ),
        "xfdesktop": _SpecParseResult(
            source_name="xfdesktop",
            build_requires=[],
            provided_binary_names={"xfdesktop"},
            build_requires_raw=[],
        ),
    }
    cap_index = _index_with(
        provides={"make"},
        source_names={"or11-backgrounds"},
    )

    with patch(
        "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
        new=AsyncMock(return_value=None),
    ), patch(
        "oreon_build.services.layer_engine.expand_chain_spec_closure_sparse",
        new=AsyncMock(
            return_value=(
                {"or11-backgrounds": "a", "xfdesktop": "b"},
                parsed,
                [],
            )
        ),
    ), patch(
        "oreon_build.services.layer_engine.providers_for_requirement",
        return_value={"xfdesktop"},
    ), patch(
        "oreon_build.services.layer_engine.get_repo_solver",
        return_value=_ok_solver(),
    ):
        deps, known, errors, unresolved = await _static_close_dependencies(
            ["or11-backgrounds"],
            cap_index=cap_index,
            **_close_kwargs(),
        )

    assert known == {"or11-backgrounds", "xfdesktop"}
    assert deps.get("or11-backgrounds", set()) == {"xfdesktop"}
    assert errors == []


@pytest.mark.asyncio
async def test_static_close_orders_runtime_requires_inside_build_set():
    """Runtime Requires only order packages already selected for build."""
    parsed = {
        "or11-backgrounds": _SpecParseResult(
            source_name="or11-backgrounds",
            build_requires=["make"],
            provided_binary_names={"or11-backgrounds"},
            build_requires_raw=["make"],
            requires=["oreon-wallpapers"],
            requires_raw=["oreon-wallpapers"],
        ),
        "oreon-wallpapers": _SpecParseResult(
            source_name="oreon-wallpapers",
            build_requires=[],
            provided_binary_names={"oreon-wallpapers"},
            build_requires_raw=[],
        ),
    }
    cap_index = _index_with(provides={"make"}, source_names=set())

    with patch(
        "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
        new=AsyncMock(return_value=None),
    ), patch(
        "oreon_build.services.layer_engine.expand_chain_spec_closure_sparse",
        new=AsyncMock(return_value=({"or11-backgrounds": "a", "oreon-wallpapers": "b"}, parsed, [])),
    ), patch(
        "oreon_build.services.layer_engine.providers_for_requirement",
        return_value={"oreon-wallpapers"},
    ), patch(
        "oreon_build.services.layer_engine.get_repo_solver",
        return_value=_ok_solver(),
    ):
        deps, known, errors, unresolved = await _static_close_dependencies(
            ["or11-backgrounds", "oreon-wallpapers"],
            cap_index=cap_index,
            **_close_kwargs(),
        )

    assert known == {"or11-backgrounds", "oreon-wallpapers"}
    assert deps.get("or11-backgrounds") == {"oreon-wallpapers"}
    assert errors == []


@pytest.mark.asyncio
async def test_static_close_does_not_overlay_blocked_provider():
    parsed = {
        "consumer": _SpecParseResult(
            source_name="consumer",
            build_requires=["provider"],
            provided_binary_names={"consumer"},
            build_requires_raw=["provider"],
        ),
        "provider": _SpecParseResult(
            source_name="provider",
            build_requires=["missing-provider-dep"],
            provided_binary_names={"provider"},
            build_requires_raw=["missing-provider-dep"],
        ),
    }
    cap_index = _index_with(provides=set(), source_names=set())
    solver = MagicMock()
    solver.resolve.side_effect = [
        MagicMock(ok=False, missing_provides={"missing-provider-dep"}),
        MagicMock(ok=False, missing_provides={"provider"}),
    ]

    with (
        patch(
            "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "oreon_build.services.layer_engine.expand_chain_spec_closure_sparse",
            new=AsyncMock(return_value=({"consumer": "a", "provider": "b"}, parsed, [])),
        ),
        patch(
            "oreon_build.services.layer_engine.providers_for_requirement",
            side_effect=lambda req, **_: {req},
        ),
        patch("oreon_build.services.layer_engine.get_repo_solver", return_value=solver),
    ):
        deps, known, errors, unresolved = await _static_close_dependencies(
            ["consumer", "provider"],
            cap_index=cap_index,
            **_close_kwargs(),
        )

    assert "provider" in deps["consumer"]
    assert unresolved["provider"] == {"missing-provider-dep"}
    assert errors == []


@pytest.mark.asyncio
async def test_static_close_preserves_solver_conflict_without_missing_capability():
    parsed = {
        "consumer": _SpecParseResult(
            source_name="consumer",
            build_requires=["provider"],
            provided_binary_names={"consumer"},
            build_requires_raw=["provider"],
        ),
    }
    cap_index = _index_with(provides={"provider"})
    solver = MagicMock()
    solver.resolve.return_value = MagicMock(
        ok=False,
        missing_provides=set(),
        problems="conflicting requests",
    )

    with (
        patch(
            "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "oreon_build.services.layer_engine.expand_chain_spec_closure_sparse",
            new=AsyncMock(return_value=({"consumer": "a"}, parsed, [])),
        ),
        patch("oreon_build.services.layer_engine.get_repo_solver", return_value=solver),
    ):
        _deps, _known, errors, unresolved = await _static_close_dependencies(
            ["consumer"],
            cap_index=cap_index,
            **_close_kwargs(),
        )

    assert errors == []
    assert unresolved["consumer"] == {"dependency solver conflict"}


@pytest.mark.asyncio
async def test_static_close_preserves_transitive_shared_library_miss():
    parsed = {
        "consumer": _SpecParseResult(
            source_name="consumer",
            build_requires=["akonadi-server-devel"],
            provided_binary_names={"consumer"},
            build_requires_raw=["akonadi-server-devel"],
        ),
    }
    cap_index = _index_with(provides={"akonadi-server-devel"})
    solver = MagicMock()
    solver.resolve.return_value = MagicMock(
        ok=False,
        missing_provides={"libQt6Qml.so.6(Qt_6.10_PRIVATE_API)(64bit)"},
    )

    with (
        patch(
            "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "oreon_build.services.layer_engine.expand_chain_spec_closure_sparse",
            new=AsyncMock(return_value=({"consumer": "a"}, parsed, [])),
        ),
        patch("oreon_build.services.layer_engine.get_repo_solver", return_value=solver),
    ):
        _deps, _known, errors, unresolved = await _static_close_dependencies(
            ["consumer"],
            cap_index=cap_index,
            **_close_kwargs(),
        )

    assert errors == []
    assert unresolved["consumer"] == {"libQt6Qml.so.6(Qt_6.10_PRIVATE_API)(64bit)"}


@pytest.mark.asyncio
async def test_static_close_pulls_versioned_unsatisfied_br_provider():
    parsed = {
        "kf6-baloo": _SpecParseResult(
            source_name="kf6-baloo",
            build_requires=["extra-cmake-modules >= 6.28.0"],
            provided_binary_names={"kf6-baloo"},
            build_requires_raw=["extra-cmake-modules >= 6.28.0"],
            version="6.28.0",
            release="1",
        ),
        "extra-cmake-modules": _SpecParseResult(
            source_name="extra-cmake-modules",
            build_requires=[],
            provided_binary_names={"extra-cmake-modules"},
            build_requires_raw=[],
            version="6.28.0",
            release="1",
        ),
    }
    cap_index = RepoCapabilityIndex(
        source_names={"extra-cmake-modules"},
        binary_names={"extra-cmake-modules"},
        provides={"extra-cmake-modules"},
        capability_evrs={"extra-cmake-modules": [("0", "6.27.0", "1.or11")]},
        binary_source={"extra-cmake-modules": "extra-cmake-modules"},
    )
    cap_index.finalize_lookups()

    with patch(
        "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
        new=AsyncMock(return_value=None),
    ), patch(
        "oreon_build.services.layer_engine.expand_chain_spec_closure_sparse",
        new=AsyncMock(return_value=({"kf6-baloo": "a", "extra-cmake-modules": "b"}, parsed, [])),
    ), patch(
        "oreon_build.services.layer_engine.providers_for_requirement",
        return_value={"extra-cmake-modules"},
    ), patch(
        "oreon_build.services.layer_engine.get_repo_solver",
        return_value=_ok_solver(),
    ):
        deps, known, errors, unresolved = await _static_close_dependencies(
            ["kf6-baloo"],
            cap_index=cap_index,
            **_close_kwargs(),
        )

    assert "extra-cmake-modules" in known
    assert deps.get("kf6-baloo") == {"extra-cmake-modules"}
    assert errors == []


def test_leaf_br_edges_prefer_repo_for_satisfied_gcc():
    from oreon_build.services.layer_engine import _add_leaf_br_edges

    parsed = _SpecParseResult(
        source_name="kf6-kauth",
        build_requires=["gcc-c++", "extra-cmake-modules >= 6.28.0"],
        provided_binary_names={"kf6-kauth"},
        build_requires_raw=["gcc-c++", "extra-cmake-modules >= 6.28.0"],
        version="6.28.0",
        release="1",
    )
    gcc = _SpecParseResult(
        source_name="gcc",
        build_requires=[],
        provided_binary_names={"gcc", "gcc-c++"},
        version="16.1.1",
        release="2",
    )
    ecm = _SpecParseResult(
        source_name="extra-cmake-modules",
        build_requires=[],
        provided_binary_names={"extra-cmake-modules"},
        version="6.28.0",
        release="1",
    )
    cap_index = RepoCapabilityIndex(
        source_names={"gcc", "extra-cmake-modules"},
        binary_names={"gcc", "gcc-c++", "extra-cmake-modules"},
        provides={"gcc", "gcc-c++", "extra-cmake-modules"},
        capability_providers={
            "gcc": {"gcc"},
            "gcc-c++": {"gcc"},
            "extra-cmake-modules": {"extra-cmake-modules"},
        },
        capability_evrs={
            "gcc": [("0", "16.1.0", "1.or11")],
            "gcc-c++": [("0", "16.1.0", "1.or11")],
            "extra-cmake-modules": [("0", "6.27.0", "1.or11")],
        },
        binary_source={
            "gcc": "gcc",
            "gcc-c++": "gcc",
            "extra-cmake-modules": "extra-cmake-modules",
        },
    )
    cap_index.finalize_lookups()
    deps: dict[str, set[str]] = {}
    _add_leaf_br_edges(
        deps,
        "kf6-kauth",
        parsed,
        provider_map={
            "gcc": {"gcc"},
            "gcc-c++": {"gcc"},
            "extra-cmake-modules": {"extra-cmake-modules"},
        },
        package_names={"gcc", "extra-cmake-modules", "kf6-kauth"},
        cap_index=cap_index,
        parsed_by_source={"kf6-kauth": parsed, "gcc": gcc, "extra-cmake-modules": ecm},
    )
    # gcc / gcc-c++ are base system build tools and must never be queued
    assert "gcc" not in deps.get("kf6-kauth", set())
    assert deps.get("kf6-kauth") == {"extra-cmake-modules"}


def test_filter_build_set_keeps_root_drops_evr_current():
    parsed = {
        "sbctl": _SpecParseResult(source_name="sbctl", build_requires=[], provided_binary_names={"sbctl"}),
        "rust-clap": _SpecParseResult(
            source_name="rust-clap",
            build_requires=[],
            provided_binary_names={"crate(clap)"},
            version="4.5.0",
            release="1",
        ),
    }
    deps = {"sbctl": {"rust-clap"}, "rust-clap": set()}
    unresolved = {"sbctl": set(), "rust-clap": set()}
    cap_index = RepoCapabilityIndex(
        source_names={"rust-clap"},
        binary_names={"rust-clap"},
        provides={"rust-clap"},
        capability_evrs={"rust-clap": [("0", "4.5.0", "1")]},
        binary_source={"rust-clap": "rust-clap"},
    )
    cap_index.finalize_lookups()
    out_deps, build_set, _un = _filter_build_set(
        ["sbctl"], deps, parsed, unresolved, cap_index
    )
    assert build_set == {"sbctl", "rust-clap"}
    assert out_deps["sbctl"] == {"rust-clap"}


def test_filter_build_set_drops_unparsed_roots():
    cap_index = _index_with()
    out_deps, build_set, _un = _filter_build_set(
        ["sbctl"], {}, {}, {}, cap_index
    )
    assert build_set == set()
    assert out_deps == {}


@pytest.mark.asyncio
async def test_static_close_unmet_crate_enqueues_only_missing_provider():
    """Unmet crate(askalono) pulls rust-askalono-cli, not repo-current rust leaves."""
    parsed = {
        "sbctl": _SpecParseResult(
            source_name="sbctl",
            build_requires=["crate(askalono)", "crate(clap)"],
            provided_binary_names={"sbctl"},
            build_requires_raw=["crate(askalono)", "crate(clap)"],
        ),
        "rust-askalono-cli": _SpecParseResult(
            source_name="rust-askalono-cli",
            build_requires=[],
            provided_binary_names={"rust-askalono-cli", "crate(askalono)"},
            build_requires_raw=[],
            version="0.5.0",
            release="1",
        ),
        "rust-clap": _SpecParseResult(
            source_name="rust-clap",
            build_requires=[],
            provided_binary_names={"rust-clap", "crate(clap)"},
            build_requires_raw=[],
            version="4.5.0",
            release="1",
        ),
    }
    cap_index = RepoCapabilityIndex(
        source_names={"rust-clap"},
        binary_names={"rust-clap", "crate(clap)"},
        provides={"rust-clap", "crate(clap)"},
        capability_providers={"crate(clap)": {"rust-clap"}, "rust-clap": {"rust-clap"}},
        capability_evrs={"rust-clap": [("0", "4.5.0", "1")], "crate(clap)": [("0", "4.5.0", "1")]},
        binary_source={"rust-clap": "rust-clap", "crate(clap)": "rust-clap"},
    )
    cap_index.finalize_lookups()

    def _providers(req, **_kw):
        if "askalono" in req:
            return {"rust-askalono-cli"}
        if "clap" in req:
            return {"rust-clap"}
        return set()

    with patch(
        "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
        new=AsyncMock(return_value=None),
    ), patch(
        "oreon_build.services.layer_engine.expand_chain_spec_closure_sparse",
        new=AsyncMock(
            return_value=(
                {
                    "sbctl": "s",
                    "rust-askalono-cli": "a",
                    "rust-clap": "c",
                },
                parsed,
                [],
            )
        ),
    ), patch(
        "oreon_build.services.layer_engine.providers_for_requirement",
        side_effect=_providers,
    ), patch(
        "oreon_build.services.layer_engine.get_repo_solver",
        return_value=_ok_solver(),
    ):
        deps, known, errors, unresolved = await _static_close_dependencies(
            ["sbctl"],
            cap_index=cap_index,
            **_close_kwargs(),
        )

    assert "rust-clap" in known
    assert known == {"sbctl", "rust-askalono-cli", "rust-clap"}
    assert deps["sbctl"] == {"rust-askalono-cli", "rust-clap"}
    assert errors == []


@pytest.mark.asyncio
async def test_autofix_still_expands_when_cap_index_lies_about_satisfied():
    """Mock already failed with nothing provides. Dont trust cap_index.satisfies."""
    plan = MagicMock()
    plan.id = 7
    plan.status = LayerPlanStatus.RUNNING
    plan.root_packages = ["sbctl"]
    plan.plan_options = {
        "architectures": ["x86_64"],
        "distgit_url": "https://github.com/oreonhq/rpm-specfiles.git",
        "distgit_branch": "main",
    }
    job = MagicMock()
    job.id = 11
    job.layer_plan_id = 7
    job.release_id = 1
    job.package_id = 2
    job.branch = "test-side"
    job.status = BuildStatus.FAILED
    job.completed_at = object()
    job.target_id = 99
    if "architecture" in getattr(job, "__dict__", {}):
        del job.__dict__["architecture"]
    release = MagicMock()
    release.id = 1
    release.releasename = "oreon-11"
    target = MagicMock()
    target.architecture = "x86_64"

    class _DB:
        async def get(self, model, ident, **kw):
            from oreon_build.models import Release as ReleaseModel

            if model is LayerPlan:
                return plan
            if model is ReleaseModel:
                return release
            if model is BuildTarget:
                return target
            return None

        async def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        async def scalar(self, stmt):
            return None

        async def flush(self):
            return None

        def add(self, obj):
            return None

        async def delete(self, obj):
            return None

    sat = _index_with(provides={"crate(askalono)"})
    with (
        patch("oreon_build.services.layer_engine.get_distgit_provides_index_cached", new=AsyncMock(return_value=None)),
        patch("oreon_build.services.layer_engine._github_headers", return_value={}),
        patch("oreon_build.services.layer_engine.load_repo_capability_index", return_value=sat) as load_idx,
        patch(
            "oreon_build.services.layer_engine.release_distgit_branch",
            return_value="main",
        ),
        patch(
            "oreon_build.services.layer_engine._provider_sources_for_miss",
            return_value={"rust-askalono"},
        ),
        patch(
            "oreon_build.services.layer_engine._planned_source_names",
            new=AsyncMock(return_value={"rust-askalono", "rust-askalono-cli"}),
        ),
    ):
        ok = await try_autofix_layer_missing_provides(
            _DB(),
            job,
            "nothing provides requested crate(askalono)",
            package_name="rust-askalono-cli",
        )
    assert ok is True
    load_idx.assert_called_once()
    assert load_idx.call_args.args[2] == ["x86_64"]
    assert job.status == BuildStatus.BLOCKED


@pytest.mark.asyncio
async def test_capability_arches_for_job_uses_target_id_not_lazy_property():
    job = MagicMock(spec=["target_id", "__dict__"])
    job.target_id = 7
    job.__dict__ = {"target_id": 7}
    target = MagicMock()
    target.architecture = "aarch64"

    class _DB:
        async def get(self, model, ident, **kw):
            assert model is BuildTarget
            assert ident == 7
            return target

    got = await _capability_arches_for_job(_DB(), job, ["x86_64", "aarch64"])
    assert got == ["aarch64"]


@pytest.mark.asyncio
async def test_autofix_uses_job_arch_not_plan_arch_intersection():
    """Plan is dual-arch but prefer-repo must check the failing job arch only."""
    plan = MagicMock()
    plan.id = 9
    plan.status = LayerPlanStatus.RUNNING
    plan.root_packages = ["sbctl"]
    plan.plan_options = {
        "architectures": ["x86_64", "aarch64"],
        "distgit_url": "https://github.com/oreonhq/rpm-specfiles.git",
        "distgit_branch": "main",
    }
    job = MagicMock()
    job.id = 12
    job.layer_plan_id = 9
    job.release_id = 1
    job.package_id = 2
    job.branch = "test-side"
    job.status = BuildStatus.FAILED
    job.target_id = 42
    release = MagicMock()
    release.id = 1
    release.releasename = "oreon-11"
    target = MagicMock()
    target.architecture = "x86_64"

    class _DB:
        async def get(self, model, ident, **kw):
            from oreon_build.models import Release as ReleaseModel

            if model is LayerPlan:
                return plan
            if model is ReleaseModel:
                return release
            if model is BuildTarget:
                return target
            return None

        async def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        async def scalar(self, stmt):
            return None

        async def flush(self):
            return None

        async def delete(self, obj):
            return None

    sat = _index_with(provides={"crate(rand)"})
    with (
        patch("oreon_build.services.layer_engine.get_distgit_provides_index_cached", new=AsyncMock(return_value=None)),
        patch("oreon_build.services.layer_engine._github_headers", return_value={}),
        patch("oreon_build.services.layer_engine.load_repo_capability_index", return_value=sat) as load_idx,
        patch("oreon_build.services.layer_engine.release_distgit_branch", return_value="main"),
        patch(
            "oreon_build.services.layer_engine._provider_sources_for_miss",
            return_value=set(),
        ),
        patch(
            "oreon_build.services.layer_engine._planned_source_names",
            new=AsyncMock(return_value=set()),
        ),
    ):
        ok = await try_autofix_layer_missing_provides(
            _DB(),
            job,
            "nothing provides requested crate(rand)",
            package_name="rust-askalono-cli",
        )
    assert ok is False
    assert job.status == BuildStatus.FAILED
    assert load_idx.call_args.args[2] == ["x86_64"]


@pytest.mark.asyncio
async def test_static_close_dependencies_pulls_transitive_install_holes():
    """Leaf BR present in repo is not enough when solv says a dep of that BR is missing."""
    parsed = {
        "sbctl": _SpecParseResult(
            source_name="sbctl",
            build_requires=["go-rpm-macros"],
            provided_binary_names={"sbctl"},
            build_requires_raw=["go-rpm-macros"],
        ),
    }
    golist_parsed = {
        "golist": _SpecParseResult(
            source_name="golist",
            build_requires=[],
            provided_binary_names={"golist"},
            build_requires_raw=[],
        ),
    }
    # golist must not look repo-current or prefer-repo skips the hole provider
    cap_index = _index_with(provides={"go-rpm-macros"}, source_names={"sbctl"})

    miss_solver = MagicMock()
    miss_solver.resolve.return_value = MagicMock(ok=False, missing_provides={"golist"})

    expand = AsyncMock(
        side_effect=[
            ({"sbctl": "spec sbctl"}, parsed, []),
            ({"golist": "spec golist"}, golist_parsed, []),
        ]
    )

    def _providers(req, **_kw):
        if req == "golist":
            return {"golist"}
        return set()

    from oreon_build.services.distgit_order import DistgitProvidesIndex

    provides_index = DistgitProvidesIndex(
        package_names=frozenset({"golist", "sbctl"}),
        capability_providers={"golist": frozenset({"golist"})},
    )

    with patch(
        "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
        new=AsyncMock(return_value=provides_index),
    ), patch(
        "oreon_build.services.layer_engine.expand_chain_spec_closure_sparse",
        new=expand,
    ), patch(
        "oreon_build.services.layer_engine.providers_for_requirement",
        side_effect=_providers,
    ), patch(
        "oreon_build.services.layer_engine.get_repo_solver",
        return_value=miss_solver,
    ):
        deps, known, errors, unresolved = await _static_close_dependencies(
            ["sbctl"],
            cap_index=cap_index,
            **_close_kwargs(),
        )

    assert "golist" in known
    assert deps["sbctl"] == {"golist"}
    assert deps["golist"] == set()
    assert errors == []
    assert unresolved.get("sbctl", set()) == set()


class _FakeDB:
    def __init__(self, *, package: Package, old_blockers: list[Any] | None = None) -> None:
        self._package = package
        self._old_blockers = old_blockers or []
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.scalar_return: Any = None

    async def get(self, model, ident):
        if model is Package:
            return self._package
        return None

    async def execute(self, stmt):
        result = MagicMock()
        result.scalars.return_value.all.return_value = self._old_blockers
        return result

    async def scalar(self, stmt):
        return self.scalar_return

    def add(self, obj) -> None:
        self.added.append(obj)

    async def delete(self, obj) -> None:
        self.deleted.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


def _fake_job(*, layer_plan_id: int = 1, package_id: int = 1, branch: str = "oreon-11-rp1") -> Any:
    job = MagicMock()
    job.id = 99
    job.package_id = package_id
    job.layer_plan_id = layer_plan_id
    job.branch = branch
    job.layer_index = 2
    job.status = BuildStatus.BLOCKED
    job.blocked_reason = "waiting on: libbar"
    return job


@pytest.mark.asyncio
async def test_recheck_job_ready_queues_when_all_deps_resolved():
    """Republish clears the block: static + dynamic deps both resolve -> job queues."""
    pkg = Package(name="topapp")
    job = _fake_job()
    db = _FakeDB(package=pkg)

    with (
        patch("oreon_build.services.layer_engine._refetch_spec_text", new=AsyncMock(return_value="Name: topapp\n")),
        patch("oreon_build.services.layer_engine._spec_parse_fn", return_value=lambda text, name: object()),
        patch("oreon_build.services.layer_engine.build_dependency_capabilities", return_value=[]),
        patch("oreon_build.services.layer_engine.runtime_dependency_capabilities", return_value=[]),
        patch(
            "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "oreon_build.services.layer_engine._resolve_install_holes",
            return_value=(set(), set()),
        ),
        patch(
            "oreon_build.services.layer_engine.prune_unneeded_layer_sources",
            new=AsyncMock(return_value=set()),
        ),
    ):
        await _recheck_job_ready(
            db,
            job,
            plan=MagicMock(id=1, plan_options={}),
            release=MagicMock(id=1),
            cap_index=RepoCapabilityIndex(),
            owner="oreonhq",
            repo="rpm-specfiles",
            distgit_branch="main",
            releasename="oreon-11",
            headers={},
            target_arches=None,
        )

    assert job.status == BuildStatus.QUEUED
    assert job.blocked_reason is None
    assert db.deleted == db._old_blockers


@pytest.mark.asyncio
async def test_recheck_job_ready_stays_blocked_on_static_hole():
    pkg = Package(name="topapp")
    job = _fake_job()
    db = _FakeDB(package=pkg)

    with (
        patch("oreon_build.services.layer_engine._refetch_spec_text", new=AsyncMock(return_value="Name: topapp\n")),
        patch("oreon_build.services.layer_engine._spec_parse_fn", return_value=lambda text, name: object()),
        patch("oreon_build.services.layer_engine.build_dependency_capabilities", return_value=["libbar-devel"]),
        patch("oreon_build.services.layer_engine.runtime_dependency_capabilities", return_value=[]),
        patch(
            "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "oreon_build.services.layer_engine._provider_sources_for_miss",
            return_value={"libbar"},
        ),
        patch(
            "oreon_build.services.layer_engine._resolve_install_holes",
            return_value=(set(), set()),
        ),
        patch(
            "oreon_build.services.layer_engine._planned_source_names",
            new=AsyncMock(return_value={"libbar", "topapp"}),
        ),
        patch(
            "oreon_build.services.layer_engine._link_planned_dep_sources",
            new=AsyncMock(return_value={"libbar"}),
        ),
        patch(
            "oreon_build.services.layer_engine._source_statuses_in_plan",
            new=AsyncMock(return_value={"libbar": [BuildStatus.QUEUED]}),
        ),
        patch(
            "oreon_build.services.layer_engine.resolve_layer_blocker_cycles",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "oreon_build.services.layer_engine.prune_unneeded_layer_sources",
            new=AsyncMock(return_value=set()),
        ),
    ):
        await _recheck_job_ready(
            db,
            job,
            plan=MagicMock(id=1, plan_options={}, triggered_by_id=1, side_tag_id=1),
            release=MagicMock(id=1, default_mock_environment_id=None),
            cap_index=RepoCapabilityIndex(),
            owner="oreonhq",
            repo="rpm-specfiles",
            distgit_branch="main",
            releasename="oreon-11",
            headers={},
            target_arches=None,
        )

    assert job.status == BuildStatus.BLOCKED
    assert "libbar" in (job.blocked_reason or "")


@pytest.mark.asyncio
async def test_on_package_published_noop_when_no_blockers_registered():
    """A publish event for a source nobody is blocked on should be a cheap no-op."""
    fake_session = _FakeDB(package=None)
    fake_session._old_blockers = []

    class _EmptyScalarsResult:
        def scalars(self):
            m = MagicMock()
            m.all.return_value = []
            return m

    async def _execute(stmt):
        return _EmptyScalarsResult()

    fake_session.execute = _execute  # type: ignore[method-assign]

    @staticmethod
    def _session_cm():
        class _Ctx:
            async def __aenter__(self):
                return fake_session

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    with patch("oreon_build.db.session.async_session_maker", side_effect=_session_cm):
        await on_package_published("libbar", "oreon-11")


@pytest.mark.asyncio
async def test_cascade_skips_missing_provides_reason():
    job = MagicMock()
    job.id = 1
    job.status = BuildStatus.FAILED
    job.layer_plan_id = 9
    job.package_id = 1
    job.blocked_reason = None
    with patch(
        "oreon_build.services.layer_plan._cancel_job_and_running_attempts",
        new=AsyncMock(),
    ) as cancel:
        await cascade_fail_dependents(
            MagicMock(),
            job,
            "rust-askalono",
            "nothing provides requested crate(foo)",
        )
    cancel.assert_not_called()
    assert job.status == BuildStatus.FAILED


@pytest.mark.asyncio
async def test_cheap_unblock_does_not_cancel_when_blocker_only_failed():
    job = MagicMock()
    job.id = 2
    job.package_id = 20
    job.target_id = 1
    job.status = BuildStatus.BLOCKED
    job.blocked_reason = "waiting on: rust-askalono"

    class _DB:
        async def execute(self, stmt):
            result = MagicMock()
            sql = str(stmt).lower()
            if "blocking_source" in sql and "layer_job_blockers" in sql:
                result.all.return_value = [("rust-askalono",)]
                return result
            if "publish_tasks" in sql or "attempts.id" in sql or "build_attempts.id" in sql:
                result.scalar.return_value = None
                result.all.return_value = []
                return result
            result.all.return_value = [("rust-askalono", BuildStatus.FAILED)]
            return result

        async def scalar(self, stmt):
            sql = str(stmt).lower()
            if "architecture" in sql:
                return "x86_64"
            if "publish_task" in sql or "publish_tasks" in sql or "build_attempts.id" in sql:
                return None
            return None

        async def delete(self, obj):
            return None

        async def flush(self):
            return None

    cascade = AsyncMock()
    with patch(
        "oreon_build.services.layer_engine.cascade_fail_dependents",
        new=cascade,
    ):
        changed = await _cheap_unblock_if_blockers_done(_DB(), job, 1113)

    assert changed is False
    assert job.status == BuildStatus.BLOCKED
    cascade.assert_not_called()


@pytest.mark.asyncio
async def test_cheap_unblock_noop_without_publish():
    """Reconciliation must not unblock dependents on SUCCESS alone; publish required."""
    job = MagicMock()
    job.id = 55
    job.package_id = 20
    job.status = BuildStatus.BLOCKED
    job.blocked_reason = "waiting on: rust-smawk"
    job.completed_at = None
    job.target_id = 1

    class _DB:
        async def execute(self, stmt):
            result = MagicMock()
            sql = str(stmt).lower()
            if "blocking_source" in sql and "layer_job_blockers" in sql:
                result.all.return_value = [("rust-smawk",)]
                return result
            if "publish_tasks" in sql or "attempts.id" in sql or "build_attempts.id" in sql:
                result.scalar.return_value = None
                result.all.return_value = []
                return result
            result.all.return_value = [("rust-smawk", BuildStatus.SUCCESS)]
            result.scalars.return_value.all.return_value = []
            return result

        async def scalar(self, stmt):
            sql = str(stmt).lower()
            if "target_id" in sql or "architecture" in sql:
                return "x86_64"
            if "publish_task" in sql or "publish_tasks" in sql or "build_attempts.id" in sql:
                return None
            return None

        async def delete(self, obj):
            return None

        async def flush(self):
            return None

    changed = await _cheap_unblock_if_blockers_done(_DB(), job, 1122)
    assert changed is False
    assert job.status == BuildStatus.BLOCKED


@pytest.mark.asyncio
async def test_autofix_hard_fails_when_providers_already_built():
    job = MagicMock()
    job.id = 77
    job.layer_plan_id = 1122
    job.release_id = 1
    job.package_id = 9
    job.branch = "plasma-6.7.4"
    job.status = BuildStatus.FAILED
    job.completed_at = None
    job.blocked_reason = None
    job.target_id = None

    plan = MagicMock()
    plan.id = 1122
    plan.status = LayerPlanStatus.RUNNING
    plan.plan_options = {
        "distgit_url": "https://github.com/oreonhq/rpm-specfiles.git",
        "distgit_branch": "testing/post-beta",
        "architectures": ["x86_64"],
    }
    plan.root_packages = []
    plan.triggered_by_id = None

    release = MagicMock()
    release.id = 1
    release.releasename = "oreon-11-rp1"

    class _DB:
        async def get(self, model, ident, **kw):
            if model is LayerPlan:
                return plan
            if model is Release:
                return release
            if model is Package:
                return Package(name="rust-textwrap")
            return None

        async def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            result.all.return_value = []
            return result

        async def delete(self, obj):
            return None

        async def flush(self):
            return None

        def add(self, obj):
            return None

    with (
        patch(
            "oreon_build.services.layer_engine.parse_missing_provides_from_text",
            return_value={"crate(smawk)"},
        ),
        patch(
            "oreon_build.services.layer_engine._misses_from_job_build_log",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "oreon_build.services.layer_engine._github_headers",
            return_value={},
        ),
        patch(
            "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "oreon_build.services.layer_engine.load_repo_capability_index",
            return_value=_index_with(),
        ),
        patch(
            "oreon_build.services.layer_engine._provider_sources_for_miss",
            return_value={"rust-smawk"},
        ),
        patch(
            "oreon_build.services.layer_engine._planned_source_names",
            new=AsyncMock(return_value={"rust-smawk", "rust-textwrap"}),
        ),
    ):
        handled = await try_autofix_layer_missing_provides(
            _DB(),
            job,
            "nothing provides requested crate(smawk)",
            package_name="rust-textwrap",
        )

    assert handled is True
    assert job.status == BuildStatus.BLOCKED
    assert "rust-smawk" in (job.blocked_reason or "")


@pytest.mark.asyncio
async def test_revive_cancelled_dependents_reblocks_chain():
    cli = MagicMock()
    cli.id = 10
    cli.status = BuildStatus.CANCELLED
    cli.blocked_reason = "upstream rust-askalono failed: boom"
    cli.package = Package(name="rust-askalono-cli")
    cli.package_id = 10
    gvt = MagicMock()
    gvt.id = 11
    gvt.status = BuildStatus.CANCELLED
    gvt.blocked_reason = "upstream rust-askalono-cli failed: upstream rust-askalono failed"
    gvt.package = Package(name="go-vendor-tools")
    gvt.package_id = 11
    added: list[Any] = []

    class _DB:
        async def execute(self, stmt):
            result = MagicMock()
            sql = str(stmt).lower()
            if "layer_job_blockers" in sql:
                result.scalars.return_value.all.return_value = []
                return result
            # revive queries cancelled jobs repeatedly by source
            jobs = []
            if "cancelled" in sql or True:
                jobs = [j for j in (cli, gvt) if j.status == BuildStatus.CANCELLED]
            result.scalars.return_value.all.return_value = jobs
            return result

        def add(self, obj):
            added.append(obj)

        async def delete(self, obj):
            return None

        async def flush(self):
            return None

    n = await _revive_upstream_cancelled_dependents(_DB(), plan_id=1113, source_name="rust-askalono")
    assert n >= 1
    assert cli.status == BuildStatus.BLOCKED
    assert "rust-askalono" in (cli.blocked_reason or "")
    assert gvt.status == BuildStatus.BLOCKED


@pytest.mark.asyncio
async def test_layer_job_failure_soft_blocks_dependents_keeps_unrelated():
    """Hard fail soft-blocks waiters. Unrelated queued jobs keep going."""
    plan = MagicMock()
    plan.id = 42
    plan.status = LayerPlanStatus.RUNNING
    plan.error_message = None

    failed = MagicMock()
    failed.id = 1
    failed.layer_plan_id = 42
    failed.package_id = 10
    failed.package = MagicMock()
    failed.package.name = "boom"
    failed.status = BuildStatus.FAILED
    failed.blocked_reason = None

    blocked = MagicMock()
    blocked.id = 2
    blocked.layer_plan_id = 42
    blocked.status = BuildStatus.BLOCKED
    blocked.blocked_reason = "waiting on: boom"
    blocked.package_id = 11
    blocked.completed_at = None

    queued = MagicMock()
    queued.id = 3
    queued.layer_plan_id = 42
    queued.status = BuildStatus.QUEUED
    queued.blocked_reason = None
    queued.package_id = 12

    class _DB:
        async def get(self, model, ident, **kw):
            if model is LayerPlan and ident == 42:
                return plan
            if model is Package and ident == 10:
                return Package(name="boom")
            return None

        async def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        async def flush(self):
            return None

    async def _cascade(db, job, package_name, reason):
        blocked.status = BuildStatus.BLOCKED
        blocked.blocked_reason = f"waiting on: {package_name} (failed)"

    async def _settle(db, layer_plan_id):
        assert queued.status == BuildStatus.QUEUED
        assert plan.status == LayerPlanStatus.RUNNING

    with (
        patch(
            "oreon_build.services.layer_engine.try_autofix_layer_missing_provides",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "oreon_build.services.layer_engine.cascade_fail_dependents",
            new=AsyncMock(side_effect=_cascade),
        ),
        patch(
            "oreon_build.services.layer_plan.maybe_settle_layer_plan",
            new=AsyncMock(side_effect=_settle),
        ),
        patch("oreon_build.services.layer_plan.notify_layer_plan_terminal") as notify,
    ):
        await on_build_job_failed_for_layer(_DB(), failed, "error: compilation failed", package_name="boom")

    assert plan.status == LayerPlanStatus.RUNNING
    assert blocked.status == BuildStatus.BLOCKED
    assert "waiting on: boom (failed)" in (blocked.blocked_reason or "")
    assert queued.status == BuildStatus.QUEUED
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_cascade_soft_blocks_direct_waiter():
    failed = MagicMock()
    failed.id = 1
    failed.status = BuildStatus.FAILED
    failed.layer_plan_id = 9
    failed.package_id = 1
    failed.blocked_reason = None

    waiter = MagicMock()
    waiter.id = 2
    waiter.status = BuildStatus.BLOCKED
    waiter.layer_plan_id = 9
    waiter.package_id = 2
    waiter.blocked_reason = "waiting on: boom"
    waiter.completed_at = None
    added: list[Any] = []

    class _DB:
        async def get(self, model, ident, **kw):
            if model is Package and ident == 1:
                return Package(name="boom")
            return None

        async def execute(self, stmt):
            result = MagicMock()
            sql = str(stmt).lower()
            if "layer_job_blockers" in sql and "blocking_source" in sql:
                if "build_job_id" in sql and "select" in sql:
                    # old blockers on failed job, or has-check, or dependent lookup
                    result.scalars.return_value.all.return_value = []
                    result.scalar = AsyncMock(return_value=1)
                    result.all.return_value = []
                    # dependent job ids from blocking_source == boom
                    bl = MagicMock()
                    bl.build_job_id = 2
                    result.scalars.return_value.all.return_value = [bl]
                    return result
            if "build_jobs" in sql:
                result.scalars.return_value.all.return_value = [waiter]
                return result
            result.scalars.return_value.all.return_value = []
            return result

        async def scalar(self, stmt):
            return 1

        def add(self, obj):
            added.append(obj)

        async def delete(self, obj):
            return None

        async def flush(self):
            return None

    await cascade_fail_dependents(_DB(), failed, "boom", "compile died")
    assert waiter.status == BuildStatus.BLOCKED
    assert "waiting on: boom (failed)" in (waiter.blocked_reason or "")


@pytest.mark.asyncio
async def test_finalize_blocked_on_dead_upstreams_closes_waiter():
    waiter = MagicMock()
    waiter.id = 2
    waiter.status = BuildStatus.BLOCKED
    waiter.blocked_reason = "waiting on: rust-winnow (failed)"
    calls = {"n": 0}

    class _DB:
        async def execute(self, stmt):
            result = MagicMock()
            calls["n"] += 1
            n = calls["n"]
            # 1: blocked jobs, 2: blocker sources, 3: blocker statuses, 4: delete blockers
            # 5: blocked jobs again (empty)
            if n == 1:
                result.scalars.return_value.all.return_value = [waiter]
            elif n == 2:
                result.all.return_value = [("rust-winnow",)]
                result.scalars.return_value.all.return_value = []
            elif n == 3:
                result.all.return_value = [(BuildStatus.FAILED,)]
            elif n == 4:
                result.scalars.return_value.all.return_value = []
            else:
                result.scalars.return_value.all.return_value = []
                result.all.return_value = []
            return result

        async def delete(self, obj):
            return None

        async def flush(self):
            return None

    n = await finalize_blocked_on_dead_upstreams(_DB(), 1114)
    assert n == 1
    assert waiter.status == BuildStatus.CANCELLED
    assert "upstream rust-winnow failed" in (waiter.blocked_reason or "")


@pytest.mark.asyncio
async def test_settle_summarizes_primary_failures():
    plan = MagicMock()
    plan.id = 9
    plan.status = LayerPlanStatus.RUNNING
    plan.error_message = None

    jobs = [
        MagicMock(status=BuildStatus.SUCCESS, package=MagicMock(name="ok"), blocked_reason=None),
        MagicMock(status=BuildStatus.FAILED, package=MagicMock(name="rust-chrono"), blocked_reason="Cargo.toml"),
        MagicMock(
            status=BuildStatus.CANCELLED,
            package=MagicMock(name="sbctl"),
            blocked_reason="upstream rust-chrono failed: Cargo.toml",
        ),
    ]
    for j, name in zip(jobs, ["ok", "rust-chrono", "sbctl"]):
        j.package.name = name

    class _DB:
        async def get(self, model, ident, **kw):
            if model is LayerPlan:
                return plan
            return None

        async def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = jobs
            return result

    with patch("oreon_build.services.layer_plan.notify_layer_plan_terminal") as notify:
        from oreon_build.services.layer_plan import maybe_settle_layer_plan

        await maybe_settle_layer_plan(_DB(), 9)

    assert plan.status == LayerPlanStatus.FAILED
    assert "1 success" in (plan.error_message or "")
    assert "1 failed" in (plan.error_message or "")
    assert "1 cancelled" in (plan.error_message or "")
    assert "Primary: rust-chrono" in (plan.error_message or "")
    notify.assert_called_once()


def test_deadlock_blocker_cycles_detects_triangle():
    cycles = deadlock_blocker_cycles(
        [1, 2, 3],
        {1: "rust-chrono", 2: "rust-chrono-tz", 3: "rust-iana-time-zone"},
        {
            1: ["rust-iana-time-zone"],
            2: ["rust-chrono"],
            3: ["rust-chrono-tz"],
        },
    )
    assert len(cycles) == 1
    assert set(cycles[0]) == {1, 2, 3}


def test_deadlock_blocker_cycles_ignores_straight_chain():
    cycles = deadlock_blocker_cycles(
        [1, 2],
        {1: "leaf", 2: "mid"},
        {1: ["mid"], 2: ["root"]},
    )
    assert cycles == []


def test_deadlock_blocker_cycles_self_loop():
    cycles = deadlock_blocker_cycles(
        [9],
        {9: "foo"},
        {9: ["foo"]},
    )
    assert cycles == [[9]]


def test_break_dep_cycles_order_breaks_mutual_wait():
    out = break_dep_cycles(
        {
            "go-vendor-tools": {"trivy"},
            "trivy": {"go-vendor-tools"},
            "golist": {"go-vendor-tools"},
            "sbctl": {"golist"},
        }
    )
    assert not (
        "trivy" in out.get("go-vendor-tools", set())
        and "go-vendor-tools" in out.get("trivy", set())
    )
    assert "golist" in out.get("sbctl", set())
    assert "go-vendor-tools" in out.get("golist", set())


def test_break_dep_cycles_prefers_repo_current_edge():
    out = break_dep_cycles(
        {
            "a": {"b"},
            "b": {"a"},
        },
        repo_current={"b"},
    )
    assert "b" not in out.get("a", set())
    assert "a" in out.get("b", set())


@pytest.mark.asyncio
async def test_resolve_layer_blocker_cycles_queues_after_order_break():
    jobs = []
    live_blockers = {
        1: ["trivy"],
        2: ["go-vendor-tools"],
        3: ["go-vendor-tools"],
        4: ["golist"],
    }
    for jid, name in (
        (1, "go-vendor-tools"),
        (2, "trivy"),
        (3, "golist"),
        (4, "sbctl"),
    ):
        j = MagicMock()
        j.id = jid
        j.status = BuildStatus.BLOCKED
        j.package = Package(name=name)
        j.package_id = jid
        j.blocked_reason = f"waiting on: {live_blockers[jid][0]}"
        jobs.append(j)

    class _DB:
        async def execute(self, stmt):
            result = MagicMock()
            sql = str(stmt).lower()
            if "blocking_source" in sql and "build_job_id" in sql:
                rows = []
                for jid, srcs in live_blockers.items():
                    for s in srcs:
                        rows.append((jid, s))
                result.all.return_value = rows
                result.scalars.return_value.all.return_value = []
                return result
            result.scalars.return_value.all.return_value = [
                j for j in jobs if j.status == BuildStatus.BLOCKED
            ]
            return result

        async def flush(self):
            return None

    async def _drop(db, job, sources):
        jid = int(job.id)
        before = list(live_blockers.get(jid, []))
        live_blockers[jid] = [s for s in before if s not in sources]
        if not live_blockers[jid]:
            job.status = BuildStatus.QUEUED
            job.blocked_reason = None
        else:
            job.blocked_reason = "waiting on: " + ", ".join(live_blockers[jid])
        return len(before) - len(live_blockers[jid])

    cascade = AsyncMock()
    with (
        patch(
            "oreon_build.services.layer_engine._drop_blocker_sources",
            new=AsyncMock(side_effect=_drop),
        ),
        patch(
            "oreon_build.services.layer_engine.cascade_fail_dependents",
            new=cascade,
        ),
    ):
        n = await resolve_layer_blocker_cycles(_DB(), 1112)

    assert n >= 1
    cascade.assert_not_called()
    assert jobs[3].status == BuildStatus.BLOCKED
    assert "golist" in (jobs[3].blocked_reason or "")
    assert any(j.status == BuildStatus.QUEUED for j in jobs[:2])

    # the member we let through must build with tests off, otherwise it still
    # BuildRequires the edge we just dropped and dies on a missing provide
    from oreon_build.services.build_pass import bootstrap_dropped_sources, job_options_dict

    freed = [j for j in jobs[:2] if j.status == BuildStatus.QUEUED]
    assert freed
    for j in freed:
        opts = job_options_dict(j)
        assert opts.get("build_profile") == "bootstrap"
        assert opts.get("rpmbuild_defines") == {"with_check": "0"}
        assert opts.get("next_profile") == "full"
        assert bootstrap_dropped_sources(j)


@pytest.mark.asyncio
async def test_settle_keeps_running_after_cycle_break_queues_work():
    plan = MagicMock()
    plan.id = 1112
    plan.status = LayerPlanStatus.RUNNING
    plan.error_message = None

    gvt = MagicMock(id=1, status=BuildStatus.BLOCKED, blocked_reason="waiting on: trivy")
    gvt.package = MagicMock()
    gvt.package.name = "go-vendor-tools"
    trivy = MagicMock(id=2, status=BuildStatus.BLOCKED, blocked_reason="waiting on: go-vendor-tools")
    trivy.package = MagicMock()
    trivy.package.name = "trivy"
    sbctl = MagicMock(id=3, status=BuildStatus.BLOCKED, blocked_reason="waiting on: golist")
    sbctl.package = MagicMock()
    sbctl.package.name = "sbctl"

    jobs_before = [gvt, trivy, sbctl]
    gvt_q = MagicMock(id=1, status=BuildStatus.QUEUED, blocked_reason=None)
    gvt_q.package = gvt.package
    jobs_after = [gvt_q, trivy, sbctl]
    calls = {"n": 0}

    class _DB:
        async def get(self, model, ident, **kw):
            if model is LayerPlan:
                return plan
            return None

        async def execute(self, stmt):
            result = MagicMock()
            calls["n"] += 1
            result.scalars.return_value.all.return_value = (
                jobs_before if calls["n"] == 1 else jobs_after
            )
            return result

    with (
        patch(
            "oreon_build.services.layer_engine.resolve_layer_blocker_cycles",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "oreon_build.services.layer_engine.finalize_blocked_on_dead_upstreams",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "oreon_build.services.layer_engine.finalize_idle_blocked_jobs",
            new=AsyncMock(return_value=0),
        ),
        patch("oreon_build.services.layer_plan.notify_layer_plan_terminal") as notify,
    ):
        from oreon_build.services.layer_plan import maybe_settle_layer_plan

        await maybe_settle_layer_plan(_DB(), 1112)

    assert plan.status == LayerPlanStatus.RUNNING
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_settle_finalizes_blocked_on_failed_then_fails_plan():
    plan = MagicMock()
    plan.id = 1114
    plan.status = LayerPlanStatus.RUNNING
    plan.error_message = None

    failed = MagicMock(
        status=BuildStatus.FAILED,
        package=MagicMock(name="rust-winnow"),
        blocked_reason="compile error",
    )
    failed.package.name = "rust-winnow"
    blocked = MagicMock(
        status=BuildStatus.BLOCKED,
        package=MagicMock(name="sbctl"),
        blocked_reason="waiting on: rust-winnow (failed)",
    )
    blocked.package.name = "sbctl"
    jobs_open = [failed, blocked]
    jobs_closed = [
        failed,
        MagicMock(
            status=BuildStatus.CANCELLED,
            package=MagicMock(name="sbctl"),
            blocked_reason="upstream rust-winnow failed",
        ),
    ]
    jobs_closed[1].package.name = "sbctl"
    calls = {"n": 0}

    class _DB:
        async def get(self, model, ident, **kw):
            if model is LayerPlan:
                return plan
            return None

        async def execute(self, stmt):
            result = MagicMock()
            calls["n"] += 1
            result.scalars.return_value.all.return_value = (
                jobs_open if calls["n"] == 1 else jobs_closed
            )
            return result

    with (
        patch(
            "oreon_build.services.layer_engine.resolve_layer_blocker_cycles",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "oreon_build.services.layer_engine.finalize_blocked_on_dead_upstreams",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "oreon_build.services.layer_engine.finalize_idle_blocked_jobs",
            new=AsyncMock(return_value=0),
        ),
        patch("oreon_build.services.layer_plan.notify_layer_plan_terminal") as notify,
    ):
        from oreon_build.services.layer_plan import maybe_settle_layer_plan

        await maybe_settle_layer_plan(_DB(), 1114)

    assert plan.status == LayerPlanStatus.FAILED
    assert "Primary: rust-winnow" in (plan.error_message or "")
    notify.assert_called_once()


@pytest.mark.asyncio
async def test_finalize_idle_blocked_fails_cannot_resolve_and_cancels_waiters():
    from oreon_build.services.layer_engine import finalize_idle_blocked_jobs

    leaf = MagicMock()
    leaf.id = 1
    leaf.status = BuildStatus.BLOCKED
    leaf.blocked_reason = "cannot resolve: cmake3 (import into distgit)"
    leaf.completed_at = None

    waiter = MagicMock()
    waiter.id = 2
    waiter.status = BuildStatus.BLOCKED
    waiter.blocked_reason = "waiting on: kf5-kcoreaddons"
    waiter.completed_at = None

    class _DB:
        async def scalar(self, stmt):
            return 0

        async def execute(self, stmt):
            result = MagicMock()
            sql = str(stmt).lower()
            if "layer_job_blockers" in sql:
                result.all.return_value = []
                result.scalars.return_value.all.return_value = []
                return result
            still = [j for j in (leaf, waiter) if j.status == BuildStatus.BLOCKED]
            result.scalars.return_value.all.return_value = still
            return result

        async def flush(self):
            return None

        async def delete(self, obj):
            return None

    with (
        patch(
            "oreon_build.services.layer_engine._cheap_unblock_if_blockers_done",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "oreon_build.services.layer_engine.finalize_blocked_on_dead_upstreams",
            new=AsyncMock(return_value=0),
        ),
    ):
        n = await finalize_idle_blocked_jobs(_DB(), 1126)

    assert n >= 2
    assert leaf.status == BuildStatus.FAILED
    assert waiter.status == BuildStatus.CANCELLED
    assert "layer idle" in (waiter.blocked_reason or "")


@pytest.mark.asyncio
async def test_autofix_missing_provides_reblocks_instead_of_halt():
    plan = MagicMock()
    plan.id = 7
    plan.status = LayerPlanStatus.RUNNING
    plan.root_packages = ["sbctl"]
    plan.plan_options = {"architectures": ["x86_64"], "distgit_url": "https://github.com/oreonhq/rpm-specfiles.git", "distgit_branch": "main"}
    plan.triggered_by_id = 1
    plan.side_tag_id = 1

    job = MagicMock()
    job.id = 11
    job.layer_plan_id = 7
    job.release_id = 1
    job.package_id = 2
    job.branch = "test-side"
    job.layer_index = 3
    job.status = BuildStatus.FAILED
    job.blocked_reason = None
    job.completed_at = object()
    job.target_id = 55
    job.job_options = {}

    release = MagicMock()
    release.id = 1
    release.releasename = "oreon-11"
    release.default_mock_environment_id = None
    target = MagicMock()
    target.architecture = "x86_64"

    class _DB:
        def __init__(self):
            self.added = []

        async def get(self, model, ident, **kw):
            if model is LayerPlan:
                return plan
            if model is Package:
                return Package(name="rust-askalono-cli")
            if model is BuildTarget:
                return target
            if model.__name__ == "Release":
                return release
            return None

        async def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        async def scalar(self, stmt):
            return None

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            return None

    db = _DB()

    with (
        patch("oreon_build.services.layer_engine.get_distgit_provides_index_cached", new=AsyncMock(return_value=None)),
        patch("oreon_build.services.layer_engine._github_headers", return_value={}),
        patch("oreon_build.services.layer_engine.load_repo_capability_index", return_value=_index_with()),
        patch(
            "oreon_build.services.layer_engine._provider_sources_for_miss",
            return_value={"rust-askalono"},
        ),
        patch(
            "oreon_build.services.layer_engine._planned_source_names",
            new=AsyncMock(return_value={"rust-askalono", "rust-askalono-cli"}),
        ),
        patch(
            "oreon_build.services.layer_engine._spawn_layer_dep_sources",
            new=AsyncMock(return_value={"rust-askalono"}),
        ),
        patch(
            "oreon_build.services.layer_engine._source_statuses_in_plan",
            new=AsyncMock(return_value={"rust-askalono": [BuildStatus.QUEUED]}),
        ),
        patch(
            "oreon_build.services.layer_engine.resolve_layer_blocker_cycles",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "oreon_build.services.layer_engine._revive_upstream_cancelled_dependents",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "oreon_build.services.layer_engine.release_distgit_branch",
            return_value="main",
        ),
        patch(
            "oreon_build.services.layer_engine.prune_unneeded_layer_sources",
            new=AsyncMock(return_value=set()),
        ),
    ):
        from oreon_build.models import Release as ReleaseModel

        async def _get(model, ident, **kw):
            if model is LayerPlan:
                return plan
            if model is Package:
                return Package(name="rust-askalono-cli")
            if model is ReleaseModel:
                return release
            return None

        db.get = _get  # type: ignore[method-assign]
        ok = await try_autofix_layer_missing_provides(
            db,
            job,
            "nothing provides requested (crate(askalono/default) >= 0.5.0 with crate(askalono/default) < 0.6.0~)",
            package_name="rust-askalono-cli",
        )

    assert ok is True
    assert job.status == BuildStatus.BLOCKED
    assert job.completed_at is None
    assert "rust-askalono" in (job.blocked_reason or "")
    assert plan.status == LayerPlanStatus.RUNNING


@pytest.mark.asyncio
async def test_autofix_reads_r2_log_when_error_message_was_head_truncated():
    plan = MagicMock()
    plan.id = 8
    plan.status = LayerPlanStatus.RUNNING
    plan.root_packages = ["sbctl"]
    plan.plan_options = {
        "architectures": ["x86_64"],
        "distgit_url": "https://github.com/oreonhq/rpm-specfiles.git",
        "distgit_branch": "main",
    }
    plan.triggered_by_id = 1
    plan.side_tag_id = 1

    job = MagicMock()
    job.id = 12
    job.layer_plan_id = 8
    job.release_id = 1
    job.package_id = 2
    job.branch = "test-side"
    job.layer_index = 1
    job.status = BuildStatus.FAILED
    job.completed_at = object()
    job.blocked_reason = None
    job.target_id = 56
    job.job_options = {}

    release = MagicMock()
    release.id = 1
    release.releasename = "oreon-11"
    release.default_mock_environment_id = None
    target = MagicMock()
    target.architecture = "x86_64"

    class _DB:
        added = []

        async def get(self, model, ident, **kw):
            from oreon_build.models import Release as ReleaseModel

            if model is LayerPlan:
                return plan
            if model is Package:
                return Package(name="rust-askalono-cli")
            if model is ReleaseModel:
                return release
            if model is BuildTarget:
                return target
            return None

        async def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        async def scalar(self, stmt):
            return "oreon-11/logs/1.log"

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            return None

    log = (
        "## mock fluff\n" * 200
        + "Problem 1: nothing provides requested (crate(askalono/default) >= 0.5.0 with crate(askalono/default) < 0.6.0~)\n"
    ).encode()

    with (
        patch("oreon_build.services.layer_engine.get_distgit_provides_index_cached", new=AsyncMock(return_value=None)),
        patch("oreon_build.services.layer_engine._github_headers", return_value={}),
        patch("oreon_build.services.layer_engine.load_repo_capability_index", return_value=_index_with()),
        patch(
            "oreon_build.services.layer_engine._provider_sources_for_miss",
            return_value={"rust-askalono"},
        ),
        patch(
            "oreon_build.services.layer_engine._planned_source_names",
            new=AsyncMock(return_value={"rust-askalono", "rust-askalono-cli"}),
        ),
        patch(
            "oreon_build.services.layer_engine._spawn_layer_dep_sources",
            new=AsyncMock(return_value={"rust-askalono"}),
        ),
        patch(
            "oreon_build.services.layer_engine._source_statuses_in_plan",
            new=AsyncMock(return_value={"rust-askalono": [BuildStatus.QUEUED]}),
        ),
        patch(
            "oreon_build.services.layer_engine.resolve_layer_blocker_cycles",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "oreon_build.services.layer_engine._revive_upstream_cancelled_dependents",
            new=AsyncMock(return_value=0),
        ),
        patch("oreon_build.services.layer_engine.release_distgit_branch", return_value="main"),
        patch(
            "oreon_build.services.layer_engine.prune_unneeded_layer_sources",
            new=AsyncMock(return_value=set()),
        ),
        patch("oreon_build.services.r2.get_r2_client") as r2_cls,
    ):
        r2_cls.return_value.get_object.return_value = log
        ok = await try_autofix_layer_missing_provides(
            _DB(),
            job,
            "## mock additional repos\n" + ("noise\n" * 100),
            package_name="rust-askalono-cli",
        )

    assert ok is True
    assert job.status == BuildStatus.BLOCKED
    assert job.completed_at is None
    assert "rust-askalono" in (job.blocked_reason or "")


def test_filter_providers_drops_mingw_for_native_consumer():
    from oreon_build.services.dep_plan.providers import (
        filter_providers_for_consumer,
        is_toolchainish_source,
        narrow_alternate_providers,
    )

    got = filter_providers_for_consumer(
        "extra-cmake-modules",
        {"qt6-qtbase", "mingw-qt6-qtbase"},
    )
    assert got == {"qt6-qtbase"}
    mingw = filter_providers_for_consumer(
        "mingw-qt6-qtdeclarative",
        {"qt6-qtbase", "mingw-qt6-qtbase"},
    )
    assert mingw == {"mingw-qt6-qtbase"}
    assert narrow_alternate_providers(
        "ghc",
        {"ghc", "ghc8.10", "ghc9.0", "ghc9.4", "ghc9.6"},
    ) == {"ghc"}
    assert is_toolchainish_source("ghc")
    assert is_toolchainish_source("ghc-base")
    assert is_toolchainish_source("ghc9.4")
    assert not is_toolchainish_source("qt6-qtbase")


def test_toolchain_any_arch_skips_ghc_libs():
    from oreon_build.services.layer_engine import (
        _source_needs_build,
        _toolchain_any_arch_satisfied,
    )

    idx = RepoCapabilityIndex()
    idx.source_names_any.add("ghc-base")
    idx.binary_names_any.add("ghc-base-devel")
    idx.capability_providers["ghc-base-devel"] = {"ghc-base"}
    idx.finalize_lookups()

    class _Restricted:
        exclusive_arches = {"x86_64"}

    restricted = {"ghc-base": _Restricted()}
    # ExclusiveArch toolchain: present on one arch is the honest answer
    assert _toolchain_any_arch_satisfied(
        idx, "ghc-base-devel", {"ghc-base"}, restricted
    )
    assert not _source_needs_build(
        "ghc-base",
        cap_index=idx,
        parsed_by_source=restricted,
        require_evr=True,
    )
    # same repo state with no ExclusiveArch is a real arch hole, so build it
    assert not _toolchain_any_arch_satisfied(idx, "ghc-base-devel", {"ghc-base"})
    assert _source_needs_build("ghc-base", cap_index=idx, require_evr=True)


def test_should_seed_blocks_foreign_compiler_stacks():
    from oreon_build.services.dep_plan.providers import (
        filter_providers_for_consumer,
        should_seed_providers,
    )

    # a non-ghc cap whose only provider is a haskell package must not drag the
    # ghc stack into a native graph
    assert not should_seed_providers("fast_float", "doctest", {"ghc-doctest"})
    assert not should_seed_providers("openjpeg", "mingw32-lcms2", {"mingw-lcms2"})
    # asking for a ghc-namespace cap means you want the haskell stack
    assert should_seed_providers("shake", "ghc-base", {"ghc"})
    assert should_seed_providers("ghc-aeson", "ghc-base-devel", {"ghc-base"})
    assert should_seed_providers("mingw-openjpeg", "mingw32-lcms2", {"mingw-lcms2"})
    assert should_seed_providers("kf6-kconfig", "cmake(Qt6Core)", {"qt6-qtbase"})
    assert should_seed_providers("plasma-workspace", "cln-devel", {"cln"})
    assert filter_providers_for_consumer(
        "fast_float",
        {"doctest", "ghc-doctest"},
    ) == {"doctest"}


def test_should_seed_blocks_base_system_packages():
    from oreon_build.services.dep_plan.providers import should_seed_providers

    # gcc and make are base system build tools — never seed them from source
    assert not should_seed_providers("kf6-kauth", "gcc", {"gcc"})
    assert not should_seed_providers("kf6-kauth", "gcc-c++", {"gcc"})
    assert not should_seed_providers("foo", "make", {"make"})
    assert not should_seed_providers("foo", "binutils", {"binutils"})
    # versioned base system reqs too
    assert not should_seed_providers("foo", "gcc >= 13", {"gcc"})
    # a non-base-system consumer that only has a base-system provider is blocked
    assert not should_seed_providers("my-app", "gcc-c++", {"gcc"})
    # but if there are non-base providers mixed in, still seed
    assert should_seed_providers("my-app", "cmake", {"cmake", "gcc"})
    # roots that ARE base system packages can still seed (e.g. building gcc itself)
    assert should_seed_providers("gcc", "gcc-c++", {"gcc"})


def test_is_base_system_package():
    from oreon_build.services.dep_plan.providers import is_base_system_package

    assert is_base_system_package("gcc")
    assert is_base_system_package("gcc-c++")
    assert is_base_system_package("make")
    assert is_base_system_package("binutils")
    assert is_base_system_package("bash")
    assert not is_base_system_package("kf6-kauth")
    assert not is_base_system_package("extra-cmake-modules")
    assert not is_base_system_package("")


def test_provider_sources_for_miss_filters_mingw():
    from oreon_build.services.layer_engine import _provider_sources_for_miss

    idx = RepoCapabilityIndex()
    idx.finalize_lookups()
    pm = {
        "cmake(Qt6Core)": {"qt6-qtbase", "mingw-qt6-qtbase"},
        "__lower__": {"cmake(qt6core)": {"qt6-qtbase", "mingw-qt6-qtbase"}},
    }
    got = _provider_sources_for_miss(
        "cmake(Qt6Core)",
        pkg="extra-cmake-modules",
        provider_map=pm,
        package_names={"qt6-qtbase", "mingw-qt6-qtbase", "extra-cmake-modules"},
        cap_index=idx,
    )
    assert got == {"qt6-qtbase"}


@pytest.mark.asyncio
async def test_autofix_spawns_provider_not_in_layer():
    job = MagicMock()
    job.id = 88
    job.layer_plan_id = 1124
    job.release_id = 1
    job.package_id = 9
    job.branch = "plasma-6.7.4"
    job.status = BuildStatus.FAILED
    job.completed_at = None
    job.blocked_reason = None
    job.target_id = None
    job.layer_index = 3

    plan = MagicMock()
    plan.id = 1124
    plan.status = LayerPlanStatus.RUNNING
    plan.plan_options = {
        "distgit_url": "https://github.com/oreonhq/rpm-specfiles.git",
        "distgit_branch": "testing/post-beta",
        "architectures": ["x86_64"],
    }
    plan.root_packages = ["plasma-workspace"]
    plan.triggered_by_id = None
    release = MagicMock()
    release.id = 1
    release.releasename = "oreon-11-rp1"

    class _DB:
        async def get(self, model, ident, **kw):
            if model is LayerPlan:
                return plan
            if model is Release:
                return release
            if model is Package:
                return Package(name="plasma-workspace")
            return None

        async def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            result.all.return_value = []
            return result

        async def flush(self):
            return None

        async def delete(self, obj):
            return None

        def add(self, obj):
            return None

    with (
        patch(
            "oreon_build.services.layer_engine.parse_missing_provides_from_text",
            return_value={"cln-devel"},
        ),
        patch(
            "oreon_build.services.layer_engine._misses_from_job_build_log",
            new=AsyncMock(return_value=set()),
        ),
        patch("oreon_build.services.layer_engine._github_headers", return_value={}),
        patch(
            "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "oreon_build.services.layer_engine.load_repo_capability_index",
            return_value=_index_with(),
        ),
        patch(
            "oreon_build.services.layer_engine._provider_sources_for_miss",
            return_value={"cln"},
        ),
        patch(
            "oreon_build.services.layer_engine._capability_arches_for_job",
            new=AsyncMock(return_value=["x86_64"]),
        ),
        patch(
            "oreon_build.services.layer_engine._planned_source_names",
            new=AsyncMock(return_value={"plasma-workspace"}),
        ),
    ):
        handled = await try_autofix_layer_missing_provides(
            _DB(),
            job,
            "nothing provides requested cln-devel",
            package_name="plasma-workspace",
        )

    assert handled is False
    assert job.status == BuildStatus.FAILED
    assert "not provided by an active layer package" in (job.blocked_reason or "")


def test_soft_retryable_blocker_reason_ignores_mock_logs():
    from oreon_build.services.layer_engine import _soft_retryable_blocker_reason

    assert _soft_retryable_blocker_reason("waiting on: cln") is True
    assert _soft_retryable_blocker_reason("expanding missing provides") is True
    assert (
        _soft_retryable_blocker_reason(
            "nothing provides mingw32-libcharset needed by freexl-devel"
        )
        is False
    )
    assert _soft_retryable_blocker_reason("## mock additional repos\nnothing provides foo") is False


@pytest.mark.asyncio
async def test_autofix_thrash_stop_skips_expand():
    job = MagicMock()
    job.id = 50644
    job.layer_plan_id = 1124
    job.release_id = 1
    job.package_id = 9
    job.branch = "plasma-6.7.4"
    job.status = BuildStatus.FAILED
    job.completed_at = None
    job.blocked_reason = None

    class _DB:
        async def get(self, model, ident, **kw):
            return None

        async def execute(self, stmt):
            result = MagicMock()
            miss = "No matching package to install: 'python3-pytest-xdist'"
            result.all.return_value = [
                (miss, BuildStatus.FAILED),
                (miss, BuildStatus.FAILED),
                (miss, BuildStatus.FAILED),
            ]
            result.scalars.return_value.all.return_value = []
            return result

        async def flush(self):
            return None

        def add(self, obj):
            return None

        async def delete(self, obj):
            return None

    with (
        patch(
            "oreon_build.services.layer_engine.parse_missing_provides_from_text",
            return_value={"python3-pytest-xdist"},
        ),
        patch(
            "oreon_build.services.layer_engine._misses_from_job_build_log",
            new=AsyncMock(return_value=set()),
        ),
    ):
        handled = await try_autofix_layer_missing_provides(
            _DB(),
            job,
            "nothing provides python3-pytest-xdist",
            package_name="linkchecker",
        )

    assert handled is False
    assert job.status == BuildStatus.FAILED
    assert "thrash stop" in (job.blocked_reason or "")


@pytest.mark.asyncio
async def test_spawn_fills_missing_plan_arches():
    job = MagicMock()
    job.id = 51097
    job.layer_plan_id = 1124
    job.branch = "plasma-6.7.4"
    job.layer_index = 3

    plan = MagicMock()
    plan.id = 1124
    plan.root_packages = ["plasma-workspace"]
    plan.plan_options = {
        "distgit_url": "https://github.com/oreonhq/rpm-specfiles.git",
        "preferred_mock_environment_id": None,
        "priority": 0,
    }
    plan.triggered_by_id = None
    plan.side_tag_id = 1

    release = MagicMock()
    release.id = 1
    release.releasename = "oreon-11-rp1"
    release.default_mock_environment_id = None

    existing = MagicMock()
    existing.id = 51168
    existing.target_id = 10
    existing.status = BuildStatus.RUNNING
    existing.blocked_reason = None

    x86 = MagicMock()
    x86.architecture = "x86_64"
    created: list[list[str]] = []
    added: list[Any] = []

    class _DB:
        async def get(self, model, ident, **kw):
            if model is BuildTarget and ident == 10:
                return x86
            return None

        async def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = [existing]
            result.all.return_value = []
            return result

        async def flush(self):
            return None

        def add(self, obj):
            added.append(obj)

    async def _create(*args, **kwargs):
        created.append(list(kwargs.get("architectures") or []))
        nj = MagicMock()
        nj.id = 999
        return [nj]

    with (
        patch(
            "oreon_build.services.layer_engine._refetch_spec_text",
            new=AsyncMock(return_value="Name: cln\n"),
        ),
        patch(
            "oreon_build.services.layer_engine._create_job_for_package",
            new=_create,
        ),
        patch(
            "oreon_build.services.distgit_order._spec_parse_fn",
            return_value=lambda text, name: _SpecParseResult(
                name="cln", epoch=0, version="1.3", release="1.or11"
            ),
        ),
    ):
        attached = await _spawn_layer_dep_sources(
            _DB(),
            job,
            plan=plan,
            release=release,
            sources={"cln"},
            owner="oreonhq",
            repo="rpm-specfiles",
            distgit_branch="testing/post-beta",
            releasename="oreon-11-rp1",
            headers={},
            arches=["x86_64", "aarch64"],
            force_build=True,
            package_names={"cln"},
        )

    assert attached == {"cln"}
    assert created == [["aarch64"]]
    assert any(isinstance(x, LayerJobBlocker) for x in added)


@pytest.mark.asyncio
async def test_force_build_sources_rebuilds_repo_current_provider():
    """autofix must rebuild a cap-gap provider even when repo EVR looks current.

    mock is authoritative that the capability is missing from the real root, so the
    repo-current EVR gate must not skip that specific provider.
    """
    job = MagicMock()
    job.id = 58503
    job.layer_plan_id = 1136
    job.branch = "testing/post-beta"
    job.layer_index = 2

    plan = MagicMock()
    plan.id = 1136
    plan.root_packages = ["perl-ExtUtils-MakeMaker-CPANfile"]
    plan.plan_options = {
        "distgit_url": "https://github.com/oreonhq/rpm-specfiles.git",
        "preferred_mock_environment_id": None,
        "priority": 0,
    }
    plan.triggered_by_id = None
    plan.side_tag_id = None

    release = MagicMock()
    release.id = 1
    release.releasename = "oreon-11-rp1"
    release.default_mock_environment_id = None

    class _DB:
        async def get(self, model, ident, **kw):
            return None

        async def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            result.all.return_value = []
            return result

        async def flush(self):
            return None

        def add(self, obj):
            pass

    created: list[str] = []

    async def _create(*args, **kwargs):
        created.append(args[2] if len(args) > 2 else kwargs.get("pkg_name"))
        nj = MagicMock()
        nj.id = 4242
        return [nj]

    cap_index = MagicMock()

    async def _run(force_sources):
        created.clear()
        with (
            patch(
                "oreon_build.services.layer_engine._refetch_spec_text",
                new=AsyncMock(return_value="Name: perl-CPAN-Meta-Requirements\n"),
            ),
            patch(
                "oreon_build.services.layer_engine._create_job_for_package",
                new=_create,
            ),
            patch(
                "oreon_build.services.layer_engine._source_needs_build",
                return_value=False,
            ),
            patch(
                "oreon_build.services.distgit_order._spec_parse_fn",
                return_value=lambda text, name: _SpecParseResult(
                    source_name="perl-CPAN-Meta-Requirements",
                    build_requires=[],
                    provided_binary_names=set(),
                    epoch=0,
                    version="2.140",
                    release="524.or11",
                ),
            ),
        ):
            return await _spawn_layer_dep_sources(
                _DB(),
                job,
                plan=plan,
                release=release,
                sources={"perl-CPAN-Meta-Requirements"},
                owner="oreonhq",
                repo="rpm-specfiles",
                distgit_branch="testing/post-beta",
                releasename="oreon-11-rp1",
                headers={},
                arches=["x86_64", "aarch64"],
                cap_index=cap_index,
                force_build=False,
                force_build_sources=force_sources,
                package_names={"perl-CPAN-Meta-Requirements"},
            )

    # repo-current provider with no force -> EVR gate skips it, autofix would die
    attached_skip = await _run(set())
    assert attached_skip == set()
    assert created == []

    # same provider marked cap-gap -> force rebuild despite repo-current EVR
    attached_force = await _run({"perl-CPAN-Meta-Requirements"})
    assert attached_force == {"perl-CPAN-Meta-Requirements"}
    assert created == ["perl-CPAN-Meta-Requirements"]


@pytest.mark.asyncio
async def test_prune_unneeded_drops_orphan_non_root():
    plan = MagicMock()
    plan.id = 1126
    plan.root_packages = ["plasma-workspace"]

    root = MagicMock()
    root.id = 1
    root.status = BuildStatus.BLOCKED
    root.blocked_reason = "waiting on: kf6-kconfig"
    root.package = Package(name="plasma-workspace")
    root.completed_at = None

    orphan = MagicMock()
    orphan.id = 2
    orphan.status = BuildStatus.PENDING
    orphan.blocked_reason = None
    orphan.package = Package(name="kactivities")
    orphan.completed_at = None

    kept_dep = MagicMock()
    kept_dep.id = 3
    kept_dep.status = BuildStatus.QUEUED
    kept_dep.blocked_reason = None
    kept_dep.package = Package(name="kf6-kconfig")
    kept_dep.completed_at = None

    jobs = [root, orphan, kept_dep]
    blockers = [LayerJobBlocker(build_job_id=1, blocking_source="kf6-kconfig")]

    class _DB:
        async def execute(self, stmt):
            result = MagicMock()
            sql = str(stmt).lower()
            if "layer_job_blockers" in sql:
                result.all.return_value = [("kf6-kconfig",)]
                result.scalars.return_value.all.return_value = []
                return result
            # open job ids
            if "build_jobs.id" in sql or sql.strip().startswith("select build_jobs.id"):
                result.scalars.return_value.all.return_value = [1, 3]
                return result
            # jobs for a package name
            result.scalars.return_value.all.return_value = [
                j for j in jobs if j.status != BuildStatus.SKIPPED or j is root
            ]
            # better: return by matching package from remaining
            if "kactivities" in sql or True:
                # return whatever still matches candidates query - filter in python
                pass
            return result

        async def flush(self):
            return None

        async def delete(self, obj):
            if obj in blockers:
                blockers.remove(obj)

    async def _exec(self, stmt):
        return await _DB.execute(self, stmt)

    class _DB2(_DB):
        async def execute(self, stmt):
            result = MagicMock()
            sql = str(stmt).lower()
            if "layer_job_blockers" in sql and "blocking_source" in sql:
                result.all.return_value = [("kf6-kconfig",)]
                result.scalars.return_value.all.return_value = []
                return result
            if "layer_job_blockers" in sql:
                result.scalars.return_value.all.return_value = []
                result.all.return_value = []
                return result
            # open ids query
            if ".id" in sql and "status" in sql and "package" not in sql:
                result.scalars.return_value.all.return_value = [
                    j.id for j in (root, orphan, kept_dep) if j.status in (
                        BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.BLOCKED, BuildStatus.RUNNING
                    )
                ]
                return result
            # package jobs query
            result.scalars.return_value.all.return_value = [
                j for j in (root, orphan, kept_dep)
                if j.package and (
                    ("kactivities" in sql and j.package.name == "kactivities")
                    or ("kf6-kconfig" in sql and j.package.name == "kf6-kconfig")
                    or j.package.name in sql
                )
            ]
            # fallback: sqlalchemy params often hide name; return both candidates' jobs
            if not result.scalars.return_value.all.return_value:
                # called per candidate name via join - can't see bind param easily
                # return based on call order via side effect below
                pass
            return result

    calls = {"n": 0}

    class _DB3:
        async def execute(self, stmt):
            result = MagicMock()
            sql = str(stmt).lower()
            if "layer_job_blockers" in sql and "blocking_source" in sql and "build_job_id" in sql:
                result.all.return_value = [("kf6-kconfig",)]
                result.scalars.return_value.all.return_value = []
                return result
            if "layer_job_blockers" in sql:
                result.scalars.return_value.all.return_value = []
                result.all.return_value = []
                return result
            if "package.name" in sql or "packages.name" in sql:
                calls["n"] += 1
                # first candidate alphabetically is kactivities, then kf6-kconfig
                result.scalars.return_value.all.return_value = (
                    [orphan] if calls["n"] == 1 else [kept_dep]
                )
                return result
            # open ids
            result.scalars.return_value.all.return_value = [1, 2, 3]
            return result

        async def flush(self):
            return None

        async def delete(self, obj):
            if obj in blockers:
                blockers.remove(obj)

    with patch(
        "oreon_build.services.layer_plan._cancel_job_and_running_attempts",
        new=AsyncMock(),
    ) as cancel:
        pruned = await prune_unneeded_layer_sources(
            _DB3(), plan, candidates={"kactivities", "kf6-kconfig"}
        )

    assert pruned == {"kactivities"}
    assert orphan.status == BuildStatus.SKIPPED
    assert "pruned:" in (orphan.blocked_reason or "")
    assert root.status == BuildStatus.BLOCKED
    assert kept_dep.status == BuildStatus.QUEUED
    cancel.assert_awaited()


@pytest.mark.asyncio
async def test_prune_noop_without_candidates():
    plan = MagicMock()
    plan.id = 1
    plan.root_packages = ["root"]
    assert await prune_unneeded_layer_sources(MagicMock(), plan) == set()
    assert await prune_unneeded_layer_sources(MagicMock(), plan, candidates=set()) == set()


def test_bcond_defines_use_rpm_bcond_macros():
    from oreon_build.worker.main import _mock_rpmbuild_define_args

    # %{?with_check} tests definedness, so "with_check 0" would switch tests ON
    flat = " ".join(_mock_rpmbuild_define_args({"with_check": "0"}))
    assert "_without_check --without-check" in flat
    assert "with_check 0" not in flat
    flat = " ".join(_mock_rpmbuild_define_args({"with_check": "1"}))
    assert "_with_check --with-check" in flat
    # plain macros are untouched
    assert "dist .or11" in " ".join(_mock_rpmbuild_define_args({"dist": ".or11"}))


def test_shell_macro_never_splits_into_phantom_caps():
    from oreon_build.core.rpm_requires import requirement_check_capabilities

    # %global majmin_ver %(echo %{version} | cut -d. -f1,2)
    caps = requirement_check_capabilities(
        "libkeduvocdocument-devel >= %(echo 26.4.3 | cut -d. -f1,2)"
    )
    assert "2)" not in caps
    assert not any(c.startswith("(echo") for c in caps)


def test_bootstrap_pass_round_trip():
    import asyncio

    from oreon_build.models import BuildStatus
    from oreon_build.services.build_pass import (
        bootstrap_dropped_sources,
        job_options_dict,
        poll_payload_from_job,
        schedule_next_pass_after_publish,
    )
    from oreon_build.services.layer_engine import _start_bootstrap_pass

    class _Job:
        def __init__(self):
            self.job_options = {}
            self.status = BuildStatus.QUEUED
            self.completed_at = "x"

    class _Db:
        async def flush(self):
            return None

    job = _Job()
    assert asyncio.run(_start_bootstrap_pass(_Db(), job, {"rust-quickcheck"}))
    opts = job_options_dict(job)
    assert opts["build_profile"] == "bootstrap"
    assert opts["rpmbuild_defines"] == {"with_check": "0"}
    assert bootstrap_dropped_sources(job) == ["rust-quickcheck"]
    # the worker gets tests turned off for the bootstrap pass
    assert poll_payload_from_job(job)["rpmbuild_defines"] == {"with_check": "0"}

    # second pass clears the defines so the rebuild actually runs its tests
    assert schedule_next_pass_after_publish(job)
    opts = job_options_dict(job)
    assert opts["build_profile"] == "full"
    assert opts["rpmbuild_defines"] == {}
    assert "next_profile" not in opts
    assert job.status == BuildStatus.PENDING
    assert poll_payload_from_job(job)["rpmbuild_defines"] == {}
    # and it does not loop back into another bootstrap
    assert not schedule_next_pass_after_publish(job)


@pytest.mark.asyncio
async def test_second_pass_waits_for_the_dep_its_bootstrap_skipped():
    from contextlib import asynccontextmanager

    from oreon_build.models import BuildStatus
    from oreon_build.services import layer_engine as le

    added: list = []

    class _Job:
        id = 5
        layer_plan_id = 77
        status = BuildStatus.PENDING
        blocked_reason = None
        job_options = {"build_profile": "full", "bootstrap_dropped": ["rust-quickcheck"]}

    job = _Job()

    class _DB:
        async def get(self, model, ident, **kw):
            return job

        async def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        def add(self, row):
            added.append(row)

        async def flush(self):
            return None

        async def commit(self):
            return None

    @asynccontextmanager
    async def _maker():
        yield _DB()

    def _session_maker():
        return _maker()

    with (
        patch("oreon_build.db.session.async_session_maker", _session_maker),
        patch.object(
            le,
            "_source_statuses_in_plan",
            new=AsyncMock(return_value={"rust-quickcheck": [BuildStatus.CANCELLED]}),
        ),
    ):
        await le.reblock_second_pass_if_needed(5)

    assert job.status == BuildStatus.BLOCKED
    assert "rust-quickcheck" in (job.blocked_reason or "")
    assert [r.blocking_source for r in added] == ["rust-quickcheck"]
    assert "bootstrap_dropped" not in job.job_options

    # once the dep really is built the full pass is free to run
    job.status = BuildStatus.PENDING
    job.job_options = {"build_profile": "full", "bootstrap_dropped": ["rust-quickcheck"]}
    added.clear()
    with (
        patch("oreon_build.db.session.async_session_maker", _session_maker),
        patch.object(
            le,
            "_source_statuses_in_plan",
            new=AsyncMock(return_value={"rust-quickcheck": [BuildStatus.SUCCESS]}),
        ),
    ):
        await le.reblock_second_pass_if_needed(5)

    assert job.status == BuildStatus.PENDING
    assert added == []


def test_ghc_lib_subpackage_provides_real_package_names():
    from oreon_build.services.distgit_order import (
        _ghc_subpackage_libname,
        _lightweight_provides_from_spec_text,
    )

    assert _ghc_subpackage_libname("base-4.20.0.0") == "base"
    # version tail is still a macro, the name is what matters
    assert _ghc_subpackage_libname("Cabal-%{Cabal_ver}") == "Cabal"
    # -c takes a dep list, never a library name
    assert _ghc_subpackage_libname("gmp-devel,libffi-devel") == ""

    spec = "\n".join(
        [
            "Name: ghc",
            "Version: 9.10.1",
            "%ghc_lib_subpackage -d -l BSD-3-Clause Cabal-%{Cabal_ver}",
            "%ghc_lib_subpackage -d -l BSD -c gmp-devel,libffi-devel base-4.20.0.0",
            "%ghc_lib_subpackage -l BSD array-0.5.8.0",
        ]
    )
    prov = _lightweight_provides_from_spec_text("ghc", spec)
    assert "ghc-base" in prov
    assert "ghc-base-devel" in prov
    assert "ghc-Cabal" in prov
    assert "ghc-Cabal-devel" in prov
    assert "ghc-array" in prov
    # no -d on array, and the -c value never becomes a package
    assert "ghc-array-devel" not in prov
    assert not [p for p in prov if "," in p]
