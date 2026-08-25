from __future__ import annotations

from oreon_build.models import BuildJob, BuildStatus
from oreon_build.services.build_pass import job_options_dict, schedule_next_pass_after_publish


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
