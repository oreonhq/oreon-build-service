from __future__ import annotations

from types import SimpleNamespace

from oreon_build.models import BuildJob, BuildStatus, LayerPlanStatus
from oreon_build.services.build_pass import job_options_dict, schedule_next_pass_after_publish
from oreon_build.services.layer_plan import layer_plan_settlement


def test_bootstrap_publish_handoff_restores_full_profile():
    job = BuildJob(
        package_id=1,
        release_id=1,
        status=BuildStatus.SUCCESS,
        job_options={
            "build_profile": "bootstrap",
            "rpmbuild_defines": {"with_check": "0"},
            "next_profile": "full",
            "bootstrap_dropped": ["provider"],
        },
    )
    assert schedule_next_pass_after_publish(job)
    assert job.status == BuildStatus.PENDING
    assert job.completed_at is None
    options = job_options_dict(job)
    assert options["build_profile"] == "full"
    assert options["rpmbuild_defines"] == {}
    assert "next_profile" not in options
    assert options["bootstrap_dropped"] == ["provider"]


def test_full_profile_without_handoff_is_unchanged():
    job = BuildJob(
        package_id=1,
        release_id=1,
        status=BuildStatus.SUCCESS,
        job_options={"build_profile": "full", "rpmbuild_defines": {}},
    )
    assert not schedule_next_pass_after_publish(job)
    assert job.status == BuildStatus.SUCCESS


def _job(status, name="pkg", reason="", blocking=None):
    pkg = SimpleNamespace(name=name)
    return SimpleNamespace(
        id=1, status=status, package=pkg, blocked_reason=reason, blocking_sources=blocking or [],
    )


def test_settlement_keeps_running_while_jobs_are_queued():
    jobs = [
        _job(BuildStatus.SUCCESS, "a"),
        _job(BuildStatus.QUEUED, "b"),
        _job(BuildStatus.BLOCKED, "c"),
    ]
    assert layer_plan_settlement(jobs) == (None, None)


def test_settlement_keeps_running_when_only_blocked_remain():
    jobs = [
        _job(BuildStatus.SUCCESS, "a"),
        _job(BuildStatus.BLOCKED, "kde", "waiting on layer jobs: foo"),
        _job(BuildStatus.BLOCKED, "bar", "waiting on repository"),
    ]
    assert layer_plan_settlement(jobs) == (None, None)


def test_layer_job_prepare_skips_unfinished_blockers():
    from oreon_build.services.layer_engine import _layer_job_needs_spec_prepare

    statuses = {("kwin", "x86_64"): {BuildStatus.BLOCKED}}
    assert _layer_job_needs_spec_prepare(["kwin"], "x86_64", statuses) is False
    assert _layer_job_needs_spec_prepare([], "x86_64", statuses) is True


def test_settlement_stops_plan_when_failures_leave_only_blocked():
    jobs = [
        _job(BuildStatus.SUCCESS, "a"),
        _job(BuildStatus.FAILED, "blosc", "mock failed"),
        _job(BuildStatus.BLOCKED, "kde", "waiting on layer jobs: blosc", ["blosc"]),
    ]
    status, msg = layer_plan_settlement(jobs)
    assert status == LayerPlanStatus.FAILED
    assert "stalled" in (msg or "")
    assert "blocked" in (msg or "")
    assert "Primary: blosc" in (msg or "")


def test_settlement_keeps_running_when_blocked_are_not_failure_fallout():
    jobs = [
        _job(BuildStatus.SUCCESS, "a"),
        _job(BuildStatus.FAILED, "hdf5", "mock failed"),
        _job(BuildStatus.BLOCKED, "kf6-ki18n", "waiting on layer jobs: qt6-qtwebengine", ["qt6-qtwebengine"]),
        _job(BuildStatus.BLOCKED, "qt6-qtwebengine", "waiting on layer jobs: python-zombie-imp", ["python-zombie-imp"]),
    ]
    assert layer_plan_settlement(jobs) == (None, None)


def test_settlement_success_when_all_ok():
    jobs = [_job(BuildStatus.SUCCESS, "a"), _job(BuildStatus.SKIPPED, "b")]
    assert layer_plan_settlement(jobs) == (LayerPlanStatus.SUCCESS, None)
