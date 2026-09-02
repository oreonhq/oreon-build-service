from __future__ import annotations

import asyncio
from types import SimpleNamespace

from oreon_build.models import BuildStatus
from oreon_build.services import layer_engine


class Solver:
    def __init__(self, value): self.value = value
    def resolve(self, requirements): return self.value


def result(ok, missing=()):
    return SimpleNamespace(ok=ok, missing_provides=set(missing), problems="")


def job():
    return SimpleNamespace(
        architecture="x86_64", branch="side-tag", status=BuildStatus.QUEUED,
        target_mock_config=None, blocked_reason=None,
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


def test_probe_routes_selected_fallback(monkeypatch):
    calls = 0
    def factory(*args, **kwargs):
        nonlocal calls
        calls += 1
        return Solver(result(False, ["snappy-devel >= 1.1"]))
    monkeypatch.setattr(layer_engine, "get_repo_solver", factory)
    candidate = job()
    assert asyncio.run(layer_engine.probe_job_target(candidate, "oreon-11-rp1", "side-tag")) == "bootstrap-custom-x86_64"
    assert calls == 1
    assert "snappy-devel >= 1.1" in candidate.blocked_reason
    assert candidate.status == BuildStatus.QUEUED


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


def test_fallback_build_is_selected_for_native_purification():
    candidate = job()
    candidate.target_mock_config = "bootstrap-custom-x86_64"
    assert layer_engine._built_on_fallback(candidate)
    candidate.target_mock_config = "oreon-11-x86_64"
    assert not layer_engine._built_on_fallback(candidate)
