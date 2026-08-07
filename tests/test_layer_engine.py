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
)
from oreon_build.services.distgit_order import _SpecParseResult
from oreon_build.services.layer_engine import (
    _capability_arches_for_job,
    _cheap_unblock_if_blockers_done,
    _filter_build_set,
    _recheck_job_ready,
    _revive_upstream_cancelled_dependents,
    _static_close_dependencies,
    break_dep_cycles,
    cascade_fail_dependents,
    deadlock_blocker_cycles,
    finalize_blocked_on_dead_upstreams,
    initial_job_status,
    on_package_published,
    parse_missing_provides_from_text,
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

    assert known == {"top"}
    assert "rust-clap" not in known
    assert deps.get("top", set()) == set()
    assert errors == []


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
    assert build_set == {"sbctl"}
    assert out_deps["sbctl"] == set()


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

    assert known == {"sbctl", "rust-askalono-cli"}
    assert "rust-clap" not in known
    assert deps["sbctl"] == {"rust-askalono-cli"}
    assert errors == []


@pytest.mark.asyncio
async def test_autofix_noop_when_miss_already_satisfied():
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
    job.target_id = 99
    # no lazy architecture property. resolve via target_id
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

        async def flush(self):
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
            "oreon_build.services.layer_engine._spawn_layer_dep_sources",
            new=AsyncMock(return_value=set()),
        ) as spawn,
    ):
        ok = await try_autofix_layer_missing_provides(
            _DB(),
            job,
            "nothing provides requested crate(askalono)",
            package_name="rust-askalono-cli",
        )
    assert ok is False
    spawn.assert_not_called()
    load_idx.assert_called_once()
    assert load_idx.call_args.args[2] == ["x86_64"]


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

        async def flush(self):
            return None

    sat = _index_with(provides={"crate(rand)"})
    with (
        patch("oreon_build.services.layer_engine.get_distgit_provides_index_cached", new=AsyncMock(return_value=None)),
        patch("oreon_build.services.layer_engine._github_headers", return_value={}),
        patch("oreon_build.services.layer_engine.load_repo_capability_index", return_value=sat) as load_idx,
        patch("oreon_build.services.layer_engine.release_distgit_branch", return_value="main"),
        patch(
            "oreon_build.services.layer_engine._spawn_layer_dep_sources",
            new=AsyncMock(return_value=set()),
        ) as spawn,
    ):
        ok = await try_autofix_layer_missing_provides(
            _DB(),
            job,
            "nothing provides requested crate(rand)",
            package_name="rust-askalono-cli",
        )
    assert ok is False
    spawn.assert_not_called()
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

    with patch(
        "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
        new=AsyncMock(return_value=None),
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
        patch("oreon_build.services.layer_engine.dependency_capabilities", return_value=[]),
        patch(
            "oreon_build.services.layer_engine.get_distgit_provides_index_cached",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "oreon_build.services.layer_engine._resolve_install_holes",
            return_value=(set(), set()),
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
        patch("oreon_build.services.layer_engine.dependency_capabilities", return_value=["libbar-devel"]),
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
            "oreon_build.services.layer_engine._spawn_layer_dep_sources",
            new=AsyncMock(return_value={"libbar"}),
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

    with (
        patch("oreon_build.services.layer_engine.clear_repo_capability_cache"),
        patch("oreon_build.db.session.async_session_maker", side_effect=_session_cm),
    ):
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
    job.status = BuildStatus.BLOCKED
    job.blocked_reason = "waiting on: rust-askalono"

    class _DB:
        async def execute(self, stmt):
            result = MagicMock()
            sql = str(stmt).lower()
            if "blocking_source" in sql and "layer_job_blockers" in sql:
                result.all.return_value = [("rust-askalono",)]
                # some dialects return rows as tuples from .all on selected columns
                if "build_job_id" not in sql:
                    result.all.return_value = [("rust-askalono",)]
                result.scalars.return_value.all.return_value = []
                return result
            # status lookup for blocker package
            result.all.return_value = [(BuildStatus.FAILED,)]
            result.scalars.return_value.all.return_value = []
            return result

        async def get(self, model, ident, **kw):
            if model is Package:
                return Package(name="rust-askalono-cli")
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
        patch("oreon_build.services.layer_plan.notify_layer_plan_terminal") as notify,
    ):
        from oreon_build.services.layer_plan import maybe_settle_layer_plan

        await maybe_settle_layer_plan(_DB(), 1114)

    assert plan.status == LayerPlanStatus.FAILED
    assert "Primary: rust-winnow" in (plan.error_message or "")
    notify.assert_called_once()


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
    new_job = MagicMock(id=99)

    with (
        patch("oreon_build.services.layer_engine.get_distgit_provides_index_cached", new=AsyncMock(return_value=None)),
        patch("oreon_build.services.layer_engine._github_headers", return_value={}),
        patch("oreon_build.services.layer_engine.load_repo_capability_index", return_value=_index_with()),
        patch(
            "oreon_build.services.layer_engine._provider_sources_for_miss",
            return_value={"rust-askalono"},
        ),
        patch(
            "oreon_build.services.layer_engine._refetch_spec_text",
            new=AsyncMock(return_value="Name: rust-askalono\nVersion: 0.5.0\nRelease: 1\n"),
        ),
        patch(
            "oreon_build.services.layer_engine._create_job_for_package",
            new=AsyncMock(return_value=[new_job]),
        ),
        patch(
            "oreon_build.services.layer_engine.release_distgit_branch",
            return_value="main",
        ),
    ):
        # Release model lookup uses oreon_build.models.Release
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
    new_job = MagicMock(id=100)

    with (
        patch("oreon_build.services.layer_engine.get_distgit_provides_index_cached", new=AsyncMock(return_value=None)),
        patch("oreon_build.services.layer_engine._github_headers", return_value={}),
        patch("oreon_build.services.layer_engine.load_repo_capability_index", return_value=_index_with()),
        patch(
            "oreon_build.services.layer_engine._provider_sources_for_miss",
            return_value={"rust-askalono"},
        ),
        patch(
            "oreon_build.services.layer_engine._refetch_spec_text",
            new=AsyncMock(return_value="Name: rust-askalono\n"),
        ),
        patch(
            "oreon_build.services.layer_engine._create_job_for_package",
            new=AsyncMock(return_value=[new_job]),
        ),
        patch("oreon_build.services.layer_engine.release_distgit_branch", return_value="main"),
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
