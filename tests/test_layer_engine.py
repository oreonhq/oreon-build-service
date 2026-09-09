from __future__ import annotations

import asyncio
from types import SimpleNamespace

from oreon_build.models import BuildStatus
from oreon_build.services import layer_engine


class Solver:
    def __init__(self, value, sources=None):
        self.value = value
        self._sources = sources or {}

    def resolve(self, requirements):
        return self.value

    def has_provider(self, cap):
        return bool(self.value.ok)

    def source_rpms_providing(self, cap):
        return set(self._sources.get(cap) or ())


def result(ok, missing=()):
    return SimpleNamespace(ok=ok, missing_provides=set(missing), problems="")


def job():
    return SimpleNamespace(
        architecture="x86_64", branch="side-tag", status=BuildStatus.QUEUED,
        target_mock_config=None, blocked_reason=None, blocking_sources=[],
        job_options={
            "solver_requirements": ["cmake"], "architecture": "x86_64",
            "native_mock_config": "oreon-11-x86_64",
            "fallback_mock_config": "bootstrap-custom-x86_64",
        },
    )


def test_probe_routes_native(monkeypatch):
    monkeypatch.setattr(layer_engine, "get_repo_solver", lambda *a, **k: Solver(result(True)))
    candidate = job()
    assert asyncio.run(layer_engine.probe_job_target(candidate, "oreon-11-rp1", "side-tag")) == "oreon-11-x86_64"


def test_cold_seed_does_not_solve_empty_requirements(monkeypatch):
    monkeypatch.setattr(
        layer_engine,
        "get_repo_solver",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("empty solve")),
    )
    candidate = job()
    candidate.job_options["solver_requirements"] = []
    target = asyncio.run(
        layer_engine.probe_job_target(candidate, "oreon-11-rp1", "side-tag")
    )
    assert target == "oreon-11-x86_64"
    assert candidate.status == BuildStatus.QUEUED


def test_probe_never_uses_fallback_mock(monkeypatch):
    monkeypatch.setattr(layer_engine, "get_repo_solver", lambda *a, **k: Solver(result(False, ["snappy-devel >= 1.1"])))
    candidate = job()
    assert asyncio.run(layer_engine.probe_job_target(candidate, "oreon-11-rp1", "side-tag")) == "oreon-11-x86_64"
    assert candidate.status == BuildStatus.BLOCKED
    assert candidate.target_mock_config == "oreon-11-x86_64"
    assert "snappy-devel >= 1.1" in candidate.blocked_reason


def test_probe_blocks_when_bridge_has_hole(monkeypatch):
    monkeypatch.setattr(layer_engine, "get_repo_solver", lambda *a, **k: Solver(result(False, ["missing-devel"])))
    candidate = job()
    candidate.job_options["fallback_mock_config"] = ""
    asyncio.run(layer_engine.probe_job_target(candidate, "oreon-11-rp1", "side-tag"))
    assert candidate.status == BuildStatus.BLOCKED
    assert candidate.blocked_reason == "waiting on repository: missing-devel"


def test_legacy_graph_scheduler_is_absent():
    assert not hasattr(layer_engine, "_evaluate_layer_buildability")
    assert not hasattr(layer_engine, "_strongly_connected_components")
    assert not hasattr(layer_engine, "_REACTIVE_REQUIREMENTS_CACHE")


def test_running_phase1_work_prevents_early_failure_settlement():
    assert BuildStatus.RUNNING in layer_engine.EXECUTING
    assert BuildStatus.QUEUED in layer_engine.EXECUTING
    assert BuildStatus.PENDING in layer_engine.EXECUTING
    assert BuildStatus.FAILED not in layer_engine.EXECUTING
    assert BuildStatus.BLOCKED not in layer_engine.EXECUTING


def test_fallback_builds_are_gone():
    candidate = job()
    candidate.target_mock_config = "bootstrap-custom-x86_64"
    assert not layer_engine._built_on_fallback(candidate)


def test_missing_native_stays_on_native(monkeypatch):
    monkeypatch.setattr(layer_engine, "get_repo_solver", lambda *a, **k: Solver(result(False, ["qt6-devel"])))
    candidate = job()
    candidate.build_phase = layer_engine.PHASE2_NATIVE
    candidate.target_mock_config = "bootstrap-custom-x86_64"
    target = asyncio.run(layer_engine.probe_job_target(candidate, "oreon-11-rp1", "side-tag"))
    assert target == candidate.target_mock_config == "oreon-11-x86_64"
    assert candidate.status == BuildStatus.BLOCKED
    assert "qt6-devel" in candidate.blocked_reason
    monkeypatch.setattr(layer_engine, "get_repo_solver", lambda *a, **k: Solver(result(True)))
    asyncio.run(layer_engine.probe_job_target(candidate, "oreon-11-rp1", "side-tag"))
    assert candidate.status == BuildStatus.QUEUED
    assert candidate.target_mock_config == "oreon-11-x86_64"


def test_pick_provider_sources_prefers_exact_name():
    picked = layer_engine.pick_provider_sources("cmake >= 3", ["extra-cmake-modules", "cmake", "cmake-gui"])
    assert picked == ["cmake"]


def test_pick_provider_sources_caps_fanout():
    names = [f"pkg{i}" for i in range(20)]
    picked = layer_engine.pick_provider_sources("virtual(foo)", names)
    assert len(picked) == layer_engine._MAX_SOURCES_PER_CAP
