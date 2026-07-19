from oreon_build.services.build_pass import job_options_dict, schedule_next_pass_after_publish
from oreon_build.models import BuildJob, BuildStatus


def test_schedule_next_pass_requeues_full() -> None:
    job = BuildJob(
        package_id=1,
        release_id=1,
        status=BuildStatus.SUCCESS,
        job_options={
            "build_profile": "bootstrap",
            "rpmbuild_defines": {"with_check": "0"},
            "next_profile": "full",
        },
    )
    assert schedule_next_pass_after_publish(job) is True
    opts = job_options_dict(job)
    assert opts.get("build_profile") == "full"
    assert opts.get("rpmbuild_defines") == {}
    assert "next_profile" not in opts
    assert job.status == BuildStatus.PENDING
