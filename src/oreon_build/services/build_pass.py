from __future__ import annotations

from typing import Any

from oreon_build.models import BuildJob, BuildStatus


def job_options_dict(job: BuildJob) -> dict[str, Any]:
    raw = getattr(job, "job_options", None)
    return dict(raw) if isinstance(raw, dict) else {}


def package_plan_from_chain(chain_options: dict[str, Any], pkg_name: str) -> dict[str, Any]:
    plans = chain_options.get("package_plans")
    if not isinstance(plans, dict):
        return {}
    hit = plans.get(pkg_name)
    return dict(hit) if isinstance(hit, dict) else {}


def apply_plan_to_job_options(job: BuildJob, plan: dict[str, Any]) -> None:
    if not plan:
        return
    opts = job_options_dict(job)
    profile = str(plan.get("build_profile") or "full").strip() or "full"
    defines = plan.get("rpmbuild_defines")
    opts["build_profile"] = profile
    opts["rpmbuild_defines"] = dict(defines) if isinstance(defines, dict) else {}
    nxt = plan.get("next_profile")
    if nxt:
        opts["next_profile"] = str(nxt)
    job.job_options = opts


def poll_payload_from_job(job: BuildJob) -> dict[str, Any]:
    opts = job_options_dict(job)
    defines = opts.get("rpmbuild_defines")
    out: dict[str, Any] = {
        "build_profile": str(opts.get("build_profile") or "full"),
        "rpmbuild_defines": dict(defines) if isinstance(defines, dict) else {},
    }
    return out


def counts_for_chain_publish(job: BuildJob) -> bool:
    opts = job_options_dict(job)
    profile = str(opts.get("build_profile") or "full").strip().lower()
    return profile == "full"


def schedule_next_pass_after_publish(job: BuildJob) -> bool:
    opts = job_options_dict(job)
    nxt = str(opts.get("next_profile") or "").strip().lower()
    if not nxt:
        return False
    opts["build_profile"] = nxt
    opts["rpmbuild_defines"] = {}
    opts.pop("next_profile", None)
    opts["pass_parent_attempt_id"] = opts.get("pass_parent_attempt_id")
    job.job_options = opts
    job.status = BuildStatus.PENDING
    job.completed_at = None
    return True
