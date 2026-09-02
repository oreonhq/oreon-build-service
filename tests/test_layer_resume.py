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


def test_bootstrap_handoff_does_not_get_clobbered_to_blocked():
    """on_package_published used to stomp a bootstrap-handoff PENDING back to
    BLOCKED, which made success counts flicker as the job re-evaluated itself.
    The function itself flips the status to PENDING; surrounding code must not
    overwrite that."""
    job = BuildJob(
        package_id=1,
        release_id=1,
        status=BuildStatus.SUCCESS,
        job_options={
            "build_profile": "bootstrap",
            "rpmbuild_defines": {"with_check": "0"},
            "next_profile": "full",
            "bootstrap_dropped": ["provider"],
            "repo_published": True,
        },
    )
    assert schedule_next_pass_after_publish(job) is True
    assert job.status == BuildStatus.PENDING
    opts = job_options_dict(job)
    assert opts["build_profile"] == "full"
    assert opts["repo_published"] is True


def _job(status, name="pkg", reason="", blocking=None, options=None):
    pkg = SimpleNamespace(name=name)
    return SimpleNamespace(
        id=1,
        status=status,
        package=pkg,
        blocked_reason=reason,
        blocking_sources=blocking or [],
        job_options=options,
    )


def test_settlement_keeps_running_while_jobs_are_queued():
    jobs = [
        _job(BuildStatus.SUCCESS, "a"),
        _job(BuildStatus.QUEUED, "b"),
        _job(BuildStatus.BLOCKED, "c"),
    ]
    assert layer_plan_settlement(jobs) == (None, None)


def test_settlement_fails_when_only_blocked_remain():
    jobs = [
        _job(BuildStatus.SUCCESS, "a"),
        _job(BuildStatus.BLOCKED, "kde", "waiting on layer jobs: foo"),
        _job(BuildStatus.BLOCKED, "bar", "waiting on repository"),
    ]
    status, msg = layer_plan_settlement(jobs)
    assert status == LayerPlanStatus.FAILED
    assert "2 blocked" in (msg or "")


def test_layer_job_prepare_skips_inflight_blockers():
    from oreon_build.services.layer_engine import _layer_job_needs_spec_prepare

    statuses = {("kwin", "x86_64"): {BuildStatus.RUNNING}}
    assert _layer_job_needs_spec_prepare(["kwin"], "x86_64", statuses) is False
    statuses = {("kwin", "x86_64"): {BuildStatus.BLOCKED}}
    assert _layer_job_needs_spec_prepare(["kwin"], "x86_64", statuses) is True
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


def test_settlement_fails_when_blocked_are_not_failure_fallout():
    jobs = [
        _job(BuildStatus.SUCCESS, "a"),
        _job(BuildStatus.FAILED, "hdf5", "mock failed"),
        _job(BuildStatus.BLOCKED, "kf6-ki18n", "waiting on layer jobs: qt6-qtwebengine", ["qt6-qtwebengine"]),
        _job(BuildStatus.BLOCKED, "qt6-qtwebengine", "waiting on layer jobs: python-zombie-imp", ["python-zombie-imp"]),
    ]
    status, msg = layer_plan_settlement(jobs)
    assert status == LayerPlanStatus.FAILED
    assert "stalled" in (msg or "")


def test_settlement_waits_for_unpublished_success():
    jobs = [
        _job(BuildStatus.SUCCESS, "provider", options={"repo_published": False}),
        _job(BuildStatus.BLOCKED, "consumer", "waiting on layer jobs: provider", ["provider"]),
    ]
    assert layer_plan_settlement(jobs) == (None, None)


def test_settlement_treats_cancelled_upstream_as_terminal():
    jobs = [
        _job(BuildStatus.CANCELLED, "provider", "Build cancelled"),
        _job(BuildStatus.BLOCKED, "consumer", "waiting on layer jobs: provider", ["provider"]),
    ]
    status, msg = layer_plan_settlement(jobs)
    assert status == LayerPlanStatus.FAILED
    assert "1 cancelled" in (msg or "")


def test_resume_refresh_schedules_layer_evaluation(monkeypatch):
    import asyncio

    scheduled = []
    monkeypatch.setattr(
        "oreon_build.services.layer_engine.schedule_layer_evaluation",
        scheduled.append,
    )
    from oreon_build.services.build_trigger_bg import run_layer_resume_refresh

    asyncio.run(run_layer_resume_refresh(1265, [1, 2, 3]))
    assert scheduled == [1265]


def test_settlement_success_when_all_ok():
    jobs = [_job(BuildStatus.SUCCESS, "a"), _job(BuildStatus.SKIPPED, "b")]
    assert layer_plan_settlement(jobs) == (LayerPlanStatus.SUCCESS, None)


def test_on_package_published_keeps_bootstrap_handoff_pending():
    """End-to-end: when on_package_published runs against a bootstrap-handoff
    job, the job must stay PENDING (so it can re-run with the full profile)
    and must not be flipped to BLOCKED."""
    from unittest.mock import AsyncMock, patch

    from oreon_build.services.layer_engine import on_package_published

    published_job = BuildJob(
        package_id=1,
        release_id=1,
        target_id=1,
        layer_plan_id=42,
        status=BuildStatus.SUCCESS,
        completed_at=None,
        job_options={
            "build_profile": "bootstrap",
            "rpmbuild_defines": {"with_check": "0"},
            "next_profile": "full",
            "bootstrap_dropped": ["peer"],
            "repo_published": True,
        },
    )
    published_job.id = 7
    fake_plan = SimpleNamespace(id=42, side_tag_id=None)

    async def fake_execute(stmt):
        class _R:
            def all(self_inner):
                return [(42,)]

            def scalars(self_inner):
                class _S:
                    def all(self_inner2):
                        return [published_job]
                return _S()

        return _R()

    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.execute = fake_execute
    fake_session.get = AsyncMock(return_value=fake_plan)
    fake_session.commit = AsyncMock()

    with patch(
        "oreon_build.db.session.async_session_maker",
        return_value=fake_session,
    ), patch(
        "oreon_build.services.layer_engine.schedule_layer_evaluation",
    ):
        import asyncio
        asyncio.run(on_package_published("pkg", "oreon-11-rp1", None, 7))

    assert published_job.status == BuildStatus.PENDING
    opts = job_options_dict(published_job)
    assert opts["build_profile"] == "full"
    assert opts["repo_published"] is True
