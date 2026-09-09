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


def _job(status, name="pkg", reason="", blocking=None, options=None, phase=None):
    pkg = SimpleNamespace(name=name)
    return SimpleNamespace(
        id=1,
        status=status,
        package=pkg,
        blocked_reason=reason,
        blocking_sources=blocking or [],
        job_options=options,
        build_phase=phase,
    )


def test_settlement_keeps_running_while_jobs_are_queued():
    jobs = [
        _job(BuildStatus.SUCCESS, "a"),
        _job(BuildStatus.QUEUED, "b"),
        _job(BuildStatus.BLOCKED, "c"),
    ]
    assert layer_plan_settlement(jobs) == (None, None)


def test_settlement_waits_when_only_blocked_remain():
    jobs = [
        _job(BuildStatus.SUCCESS, "a"),
        _job(BuildStatus.BLOCKED, "kde", "waiting on layer jobs: foo"),
        _job(BuildStatus.BLOCKED, "bar", "waiting on repository"),
    ]
    assert layer_plan_settlement(jobs) == (None, None)


def test_settlement_waits_while_repository_jobs_are_blocked():
    jobs = [
        _job(BuildStatus.SUCCESS, "a"),
        _job(BuildStatus.FAILED, "blosc", "mock failed"),
        _job(BuildStatus.BLOCKED, "kde", "waiting on layer jobs: blosc", ["blosc"]),
    ]
    assert layer_plan_settlement(jobs) == (None, None)


def test_settlement_waits_when_unrelated_jobs_are_blocked():
    jobs = [
        _job(BuildStatus.SUCCESS, "a"),
        _job(BuildStatus.FAILED, "hdf5", "mock failed"),
        _job(BuildStatus.BLOCKED, "kf6-ki18n", "waiting on layer jobs: qt6-qtwebengine", ["qt6-qtwebengine"]),
        _job(BuildStatus.BLOCKED, "qt6-qtwebengine", "waiting on layer jobs: python-zombie-imp", ["python-zombie-imp"]),
    ]
    assert layer_plan_settlement(jobs) == (None, None)


def test_settlement_waits_for_unpublished_success():
    jobs = [
        _job(BuildStatus.SUCCESS, "provider", options={"repo_published": False}),
        _job(BuildStatus.BLOCKED, "consumer", "waiting on layer jobs: provider", ["provider"]),
    ]
    assert layer_plan_settlement(jobs) == (None, None)


def test_settlement_waits_for_blocked_after_cancelled_job():
    jobs = [
        _job(BuildStatus.CANCELLED, "provider", "Build cancelled"),
        _job(BuildStatus.BLOCKED, "consumer", "waiting on layer jobs: provider", ["provider"]),
    ]
    assert layer_plan_settlement(jobs) == (None, None)


def test_phase1_failure_settles_when_every_job_is_terminal():
    jobs = [
        _job(BuildStatus.SUCCESS, "provider", phase="phase1_bridge"),
        _job(BuildStatus.FAILED, "consumer", "mock failed", phase="phase1_bridge"),
    ]
    status, msg = layer_plan_settlement(jobs)
    assert status == LayerPlanStatus.FAILED
    assert "consumer" in (msg or "")


def test_successful_native_plan_settles():
    jobs = [
        _job(BuildStatus.SUCCESS, "provider", phase="phase1_bridge"),
        _job(BuildStatus.SUCCESS, "consumer", phase="phase1_bridge"),
    ]
    assert layer_plan_settlement(jobs) == (LayerPlanStatus.SUCCESS, None)


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


def test_native_builddep_failure_waits_instead_of_repeating_fallback(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from oreon_build.services import layer_plan

    job = BuildJob(
        id=1, package_id=1, release_id=1, layer_plan_id=1275,
        status=BuildStatus.FAILED, build_phase="phase2_native",
        target_mock_config="oreon-11-rp1-aarch64",
        job_options={"native_mock_config": "oreon-11-rp1-aarch64", "fallback_mock_config": "fedora-44-aarch64"},
    )
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(id=1275, status=LayerPlanStatus.RUNNING)
    monkeypatch.setattr(layer_plan, "maybe_settle_layer_plan", AsyncMock())
    asyncio.run(layer_plan.on_layer_resolve_failed(db, job, "missing dependencies", missing_provides=["qt6-devel"]))
    assert job.status == BuildStatus.BLOCKED
    assert job.target_mock_config == "oreon-11-rp1-aarch64"
    assert "qt6-devel" in job.blocked_reason


def test_installdeps_uses_srpm_requires_not_log_text(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from oreon_build.services import layer_plan

    job = BuildJob(
        id=1, package_id=1, release_id=1, layer_plan_id=1275,
        status=BuildStatus.FAILED,
        target_mock_config="oreon-11-x86_64",
        job_options={"native_mock_config": "oreon-11-x86_64"},
    )
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(id=1275, status=LayerPlanStatus.RUNNING)
    monkeypatch.setattr(layer_plan, "maybe_settle_layer_plan", AsyncMock())
    reason = "mock --installdeps failed\nNo matching package to install: 'cmake(OpenCV)'\n"
    asyncio.run(
        layer_plan.on_layer_resolve_failed(
            db,
            job,
            reason,
            source_requirements=["cmake(KQuickImageEditor)", "cmake(OpenCV)", "rpmlib(CompressedFileNames)"],
        )
    )
    assert job.status == BuildStatus.BLOCKED
    assert "cmake(OpenCV)" in job.blocked_reason
    assert "cmake(KQuickImageEditor)" in job.blocked_reason


def test_resume_preserves_success_after_earlier_failed_attempt(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from oreon_build.services import layer_plan, retry_eligibility
    from oreon_build.models import BuildAttempt

    plan = SimpleNamespace(id=1275, status=LayerPlanStatus.FAILED, plan_options={})
    succeeded = BuildJob(id=1, package_id=1, release_id=1, status=BuildStatus.SUCCESS)
    succeeded.attempts = [
        BuildAttempt(id=12, build_job_id=1, attempt_number=2, status=BuildStatus.SUCCESS),
        BuildAttempt(id=11, build_job_id=1, attempt_number=1, status=BuildStatus.FAILED),
    ]
    failed = BuildJob(id=2, package_id=2, release_id=1, status=BuildStatus.FAILED)

    class Rows:
        def __init__(self, values): self.values = values
        def scalar_one_or_none(self): return self.values[0]
        def scalars(self): return self
        def all(self): return self.values

    db = AsyncMock()
    db.execute.side_effect = [Rows([plan]), Rows([succeeded, failed]), Rows([]), Rows([])]
    eligibility = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(retry_eligibility, "source_failure_can_retry", eligibility)
    monkeypatch.setattr(layer_plan, "log_audit", AsyncMock())
    monkeypatch.setattr(layer_plan, "get_client_ip", lambda request: None)
    monkeypatch.setattr(layer_plan, "get_user_agent", lambda request: None)
    _, reopened = asyncio.run(layer_plan.resume_layer_plan_by_user(db, None, SimpleNamespace(id=1), layer_plan_id=1275))
    assert reopened == [2]
    assert succeeded.status == BuildStatus.SUCCESS
    assert failed.status == BuildStatus.BLOCKED
    eligibility.assert_awaited_once_with(db, failed, None)
