from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status
from sqlalchemy import case, func, select, update
from sqlalchemy.orm import selectinload

from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.models import (
    BuildAttempt,
    BuildJob,
    BuildStatus,
    LayerPlan,
    LayerPlanStatus,
    Package,
    PackageVersion,
    Source,
)
from oreon_build.services.distgit_order import db_safe_spec_evr
from oreon_build.services.ci_callback import notify_layer_plan_terminal

_RE_UPSTREAM_FAILED = re.compile(r"^upstream\s+(\S+)\s+failed\b", re.IGNORECASE)
_RE_WAITING_TRAIL = re.compile(r"\s*\([^)]*\)\s*$")
if TYPE_CHECKING:
    from oreon_build.models import Account

logger = logging.getLogger(__name__)

DEFAULT_ARCH = ["x86_64", "aarch64"]


def distgit_new_package_dict(
    pkg: str,
    releasename: str,
    *,
    distgit_url: str,
    distgit_branch: str,
    preferred_mock_environment_id: int | None,
    spec_version: str | None = None,
) -> dict[str, Any]:
    spec_rel = f"{releasename}/{pkg}/{pkg}.spec"
    url = f"{distgit_url.rstrip('/')}#{distgit_branch}:{spec_rel}"
    return {
        "name": pkg,
        "description": None,
        "preferred_mock_environment_id": preferred_mock_environment_id,
        "github_repository_id": None,
        "github_html_url": "https://github.com/oreonhq/rpm-specfiles",
        "source_type": "distgit",
        "source_url": url,
        "source_urls": None,
        "source_branch": distgit_branch,
        "source_path": spec_rel,
        "version": db_safe_spec_evr((spec_version or "0.0.1").strip() or "0.0.1"),
    }


def infer_github_html_url_from_distgit(url: str) -> str | None:
    if not url or "github.com" not in url.lower():
        return None
    base = url.split("#", 1)[0].strip().rstrip("/")
    if base.lower().endswith(".git"):
        base = base[:-4]
    if not base.lower().startswith("http"):
        return None
    return base


async def ensure_spec_package_version(
    db: Any,
    package_id: int,
    spec_canonical: str | None,
) -> int | None:
    from oreon_build.services.build_target_util import latest_package_version_id

    if not spec_canonical:
        return await latest_package_version_id(db, package_id)
    spec_canonical = db_safe_spec_evr(spec_canonical)
    row = await db.execute(
        select(PackageVersion.id)
        .where(
            PackageVersion.package_id == package_id,
            PackageVersion.version == spec_canonical,
        )
        .order_by(PackageVersion.id.desc())
        .limit(1)
    )
    vid = row.scalar_one_or_none()
    if vid is not None:
        return int(vid)
    pv = PackageVersion(package_id=package_id, version=spec_canonical)
    db.add(pv)
    await db.flush()
    return pv.id


async def ensure_distgit_source_on_package_version(
    db: Any,
    package_version_id: int | None,
    *,
    pkg_name: str,
    releasename: str,
    distgit_url: str,
    distgit_branch: str,
) -> None:
    if package_version_id is None or not releasename.strip():
        return
    want_url = distgit_new_package_dict(
        pkg_name,
        releasename,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        preferred_mock_environment_id=None,
    )["source_url"]
    row = await db.execute(
        select(Source)
        .where(
            Source.package_version_id == package_version_id,
            Source.kind == "distgit",
        )
        .limit(1)
    )
    src = row.scalar_one_or_none()
    if src is None:
        db.add(Source(package_version_id=package_version_id, kind="distgit", url=want_url))
        await db.flush()
        return
    if (src.url or "").strip() != want_url:
        src.url = want_url
        await db.flush()


async def get_or_create_package(
    db: Any,
    pkg_name: str,
    *,
    release_id: int,
    releasename: str,
    distgit_url: str,
    distgit_branch: str,
    preferred_mock_environment_id: int | None,
    triggered_by_id: int | None,
    request: Request | None,
    layer_plan_ids: list[int] | None = None,
    skip_audit: bool = False,
) -> tuple[int, int | None]:
    """Returns (package_id, package_version_id)."""
    from oreon_build.services.build_target_util import resolve_package_by_name

    existing_pkg = await resolve_package_by_name(
        db, pkg_name, release_id=release_id, layer_plan_ids=layer_plan_ids
    )
    if existing_pkg:
        package_id = existing_pkg.id
        package_version_id = await ensure_spec_package_version(db, package_id, None)
        if releasename:
            await ensure_distgit_source_on_package_version(
                db,
                package_version_id,
                pkg_name=pkg_name,
                releasename=releasename,
                distgit_url=distgit_url,
                distgit_branch=distgit_branch,
            )
        return package_id, package_version_id

    npd = distgit_new_package_dict(
        pkg_name,
        releasename,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        preferred_mock_environment_id=preferred_mock_environment_id,
    )
    gh_url = infer_github_html_url_from_distgit(npd["source_url"] or "")
    pkg = Package(
        name=npd["name"],
        description=npd["description"],
        preferred_mock_environment_id=npd["preferred_mock_environment_id"],
        github_repository_id=npd["github_repository_id"],
        github_html_url=gh_url,
    )
    db.add(pkg)
    pv = PackageVersion(package=pkg, version=npd["version"] or "0.0.1")
    db.add(pv)
    if npd.get("source_url"):
        db.add(Source(package_version=pv, kind="distgit", url=npd["source_url"]))
    await db.flush()
    package_id = pkg.id
    package_version_id = pv.id
    if triggered_by_id is not None and not skip_audit:
        await log_audit(
            db,
            "package.create",
            account_id=triggered_by_id,
            resource_type="package",
            resource_id="pending",
            details=pkg.name,
            ip_address=get_client_ip(request) if request is not None else None,
            user_agent=get_user_agent(request) if request is not None else "layer-build",
            emit_discord=False,
        )
    return package_id, package_version_id


def _job_build_terminal_ok(status_: BuildStatus) -> bool:
    return status_ in (BuildStatus.SUCCESS, BuildStatus.SKIPPED)


_IN_FLIGHT_STATUSES = (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)


def _is_failure_fallout(job: Any, failed_names: set[str]) -> bool:
    reason = (getattr(job, "blocked_reason", None) or "").strip()
    if reason.lower().startswith("upstream failure:"):
        return True
    failed_l = {str(n).strip().lower() for n in failed_names if str(n).strip()}
    sources: set[str] = set()
    for raw in getattr(job, "blocking_sources", None) or []:
        name = str(raw or "").strip().lower()
        if name.endswith(".spec"):
            name = name[:-5]
        if name:
            sources.add(name)
    return bool(sources) and bool(failed_l) and sources <= failed_l


def layer_plan_settlement(
    jobs: list[Any],
) -> tuple[LayerPlanStatus | None, str | None]:
    """How a plan should look once nothing is in flight.

    Blocked jobs are waiters, not work. Failures only stop the plan when
    every remaining waiter is fallout of those failures. Other blocked
    jobs can still SAT-queue.
    """
    if not jobs:
        return None, None
    if any(j.status in _IN_FLIGHT_STATUSES for j in jobs):
        return None, None
    n_ok = sum(1 for j in jobs if _job_build_terminal_ok(j.status))
    n_fail = sum(1 for j in jobs if j.status == BuildStatus.FAILED)
    n_cancel = sum(1 for j in jobs if j.status == BuildStatus.CANCELLED)
    n_block = sum(1 for j in jobs if j.status == BuildStatus.BLOCKED)
    if n_block == 0 and n_fail == 0 and n_cancel == 0:
        return LayerPlanStatus.SUCCESS, None
    if n_fail == 0 and n_cancel == 0:
        return None, None
    failed_names = set()
    for j in jobs:
        if j.status != BuildStatus.FAILED:
            continue
        name = j.package.name if getattr(j, "package", None) else ""
        if name:
            failed_names.add(str(name).strip())
    if n_block > 0 and any(
        not _is_failure_fallout(j, failed_names)
        for j in jobs
        if j.status == BuildStatus.BLOCKED
    ):
        return None, None
    primary: list[str] = []
    seen: set[str] = set()
    for j in jobs:
        if j.status != BuildStatus.FAILED:
            continue
        name = j.package.name if getattr(j, "package", None) else f"job-{j.id}"
        if name in seen:
            continue
        reason = (getattr(j, "blocked_reason", None) or "").strip()
        if reason.startswith("upstream ") or reason.startswith("Layer plan stopped:"):
            continue
        if reason.startswith("deadlock:") or reason.startswith("cycle "):
            continue
        if reason.startswith("waiting on:") or reason.startswith("waiting on "):
            continue
        seen.add(name)
        primary.append(name)
    bits = [f"{n_ok} success", f"{n_fail} failed", f"{n_block} blocked", f"{n_cancel} cancelled"]
    if n_block > 0:
        msg = "Layer stalled with no queued or running jobs (" + ", ".join(bits) + ")."
    else:
        msg = "Layer finished with failures (" + ", ".join(bits) + ")."
    if primary:
        msg += " Primary: " + ", ".join(primary)
    return LayerPlanStatus.FAILED, msg


async def _stop_job_and_running_attempts(
    db: Any,
    job: BuildJob,
    note: str,
    *,
    as_status: BuildStatus,
) -> None:
    if job.status in (BuildStatus.SUCCESS, BuildStatus.SKIPPED, BuildStatus.FAILED, BuildStatus.CANCELLED):
        return
    job.status = as_status
    job.blocked_reason = (note or "")[:500] or job.blocked_reason
    att_r = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.build_job_id == job.id,
            BuildAttempt.status.in_((BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)),
        )
    )
    att_status = BuildStatus.CANCELLED if as_status == BuildStatus.CANCELLED else BuildStatus.FAILED
    for att in att_r.scalars().all():
        att.status = att_status
        if not (att.error_message or "").strip():
            att.error_message = note


async def _cancel_job_and_running_attempts(db: Any, job: BuildJob, note: str) -> None:
    await _stop_job_and_running_attempts(db, job, note, as_status=BuildStatus.CANCELLED)


async def fail_layer_plan(
    db: Any,
    plan: LayerPlan,
    message: str,
    *,
    terminal: LayerPlanStatus = LayerPlanStatus.FAILED,
) -> None:
    plan.status = terminal
    plan.error_message = (message or terminal.value)[:8000]
    open_job_status = (
        BuildStatus.CANCELLED if terminal == LayerPlanStatus.CANCELLED else BuildStatus.FAILED
    )
    note = ("Layer plan stopped: " + (message or terminal.value))[:500]
    open_states = (
        BuildStatus.PENDING,
        BuildStatus.QUEUED,
        BuildStatus.BLOCKED,
        BuildStatus.RUNNING,
    )
    await db.execute(
        update(BuildJob)
        .where(
            BuildJob.layer_plan_id == plan.id,
            BuildJob.status.in_(open_states),
        )
        .values(status=open_job_status, blocked_reason=note)
    )
    att_status = (
        BuildStatus.CANCELLED if terminal == LayerPlanStatus.CANCELLED else BuildStatus.FAILED
    )
    open_job_ids = select(BuildJob.id).where(BuildJob.layer_plan_id == plan.id)
    await db.execute(
        update(BuildAttempt)
        .where(
            BuildAttempt.build_job_id.in_(open_job_ids),
            BuildAttempt.status.in_(open_states),
        )
        .values(
            status=att_status,
            error_message=case(
                (
                    (BuildAttempt.error_message.is_(None))
                    | (func.trim(BuildAttempt.error_message) == ""),
                    note,
                ),
                else_=BuildAttempt.error_message,
            ),
        )
    )
    await db.flush()
    notify_layer_plan_terminal(plan.id, terminal.value, plan.error_message)


async def on_layer_resolve_failed(
    db: Any,
    job: BuildJob,
    reason: str,
    *,
    package_name: str | None = None,
    missing_provides: list[str] | None = None,
) -> None:
    if job.layer_plan_id is None:
        return
    plan = await db.get(LayerPlan, job.layer_plan_id)
    if plan is None or plan.status in (LayerPlanStatus.SUCCESS, LayerPlanStatus.FAILED, LayerPlanStatus.CANCELLED):
        return
    from oreon_build.core.dnf_missing import parse_dnf_missing_provides
    from oreon_build.core.rpm_requires import is_malformed_rpm_requirement
    from oreon_build.services.build_pass import job_options_dict
    from oreon_build.services.layer_engine import (
        evaluate_layer_buildability,
        invalidate_reactive_requirements_for_package,
    )

    caps: list[str] = []
    seen: set[str] = set()
    for item in list(missing_provides or []) + parse_dnf_missing_provides(reason or ""):
        cap = str(item or "").strip()
        if not cap or cap in seen:
            continue
        seen.add(cap)
        caps.append(cap)
    if not caps or any(is_malformed_rpm_requirement(cap) for cap in caps):
        await on_build_job_failed_for_layer(
            db, job, reason, package_name=package_name,
        )
        return
    invalidate_reactive_requirements_for_package(package_name or "")
    opts = job_options_dict(job)
    opts["sat_hold_caps"] = caps
    job.job_options = opts
    job.status = BuildStatus.BLOCKED
    job.completed_at = None
    job.blocked_reason = "waiting on repository: " + ", ".join(caps)[:480]
    job.blocking_sources = []
    await db.flush()
    await evaluate_layer_buildability(db, int(plan.id))
    await maybe_settle_layer_plan(db, int(plan.id))


async def on_build_job_failed_for_layer(
    db: Any,
    job: BuildJob,
    reason: str,
    *,
    package_name: str | None = None,
) -> None:
    if job.layer_plan_id is None:
        return
    plan = await db.get(LayerPlan, job.layer_plan_id)
    if plan is None or plan.status in (LayerPlanStatus.SUCCESS, LayerPlanStatus.FAILED, LayerPlanStatus.CANCELLED):
        return
    job.status = BuildStatus.FAILED
    job.completed_at = datetime.now(timezone.utc)
    job.blocked_reason = None
    job.error_message = (reason or "Build failed")[:8000]
    await db.flush()
    from oreon_build.services.layer_engine import evaluate_layer_buildability

    await evaluate_layer_buildability(db, int(plan.id))
    await maybe_settle_layer_plan(db, int(plan.id))



async def on_user_cancelled_layer_job(db: Any, job: BuildJob) -> None:
    return


async def cancel_layer_plan_by_user(
    db: Any,
    request: Request,
    user: "Account",
    *,
    layer_plan_id: int,
) -> LayerPlan:
    result = await db.execute(
        update(LayerPlan)
        .where(
            LayerPlan.id == layer_plan_id,
            LayerPlan.status.in_((LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING)),
        )
        .values(
            status=LayerPlanStatus.CANCELLED,
            error_message="Cancelled by user",
        )
        .returning(LayerPlan)
    )
    plan = result.scalar_one_or_none()
    if not plan:
        existing = await db.get(LayerPlan, layer_plan_id)
        if not existing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layer plan not found")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Layer plan is already {existing.status.value}",
        )
    await db.execute(
        update(BuildJob)
        .where(
            BuildJob.layer_plan_id == layer_plan_id,
            BuildJob.status.in_((BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.BLOCKED, BuildStatus.RUNNING)),
        )
        .values(
            status=BuildStatus.CANCELLED,
            blocked_reason="Layer plan stopped: Cancelled by user",
        )
    )
    await db.execute(
        update(BuildAttempt)
        .where(
            BuildAttempt.build_job_id.in_(select(BuildJob.id).where(BuildJob.layer_plan_id == layer_plan_id)),
            BuildAttempt.status.in_((BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)),
        )
        .values(status=BuildStatus.CANCELLED, error_message="Cancelled by user")
    )
    await log_audit(
        db,
        "build.layer_cancel",
        account_id=user.id,
        resource_type="layer_plan",
        resource_id=str(plan.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return plan




async def resume_layer_plan_by_user(
    db: Any,
    request: Request,
    user: "Account",
    *,
    layer_plan_id: int,
) -> tuple[LayerPlan, list[int]]:
    result = await db.execute(
        select(LayerPlan).where(LayerPlan.id == layer_plan_id)
    )
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layer plan not found")
    if plan.status == LayerPlanStatus.SUCCESS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Layer plan already succeeded",
        )
    jobs_r = await db.execute(
        select(BuildJob).where(BuildJob.layer_plan_id == plan.id)
    )
    jobs = list(jobs_r.scalars().all())
    live_ids = [
        int(j.id)
        for j in jobs
        if j.status in (BuildStatus.QUEUED, BuildStatus.PENDING, BuildStatus.RUNNING)
    ]
    live_attempt_jobs: set[int] = set()
    if live_ids:
        att_r = await db.execute(
            select(BuildAttempt.build_job_id).where(
                BuildAttempt.build_job_id.in_(live_ids),
                BuildAttempt.status.in_(
                    (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)
                ),
            )
        )
        live_attempt_jobs = {int(jid) for (jid,) in att_r.all()}
    real_inflight = [
        j
        for j in jobs
        if j.status in (BuildStatus.QUEUED, BuildStatus.PENDING)
        or (j.status == BuildStatus.RUNNING and int(j.id) in live_attempt_jobs)
    ]
    if plan.status not in (LayerPlanStatus.FAILED, LayerPlanStatus.CANCELLED) and real_inflight:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Layer still has {len(real_inflight)} queued or running jobs. "
                "Cancel the layer first if you want a full retry."
            ),
        )
    reopen = [
        j
        for j in jobs
        if j.status not in (BuildStatus.SUCCESS, BuildStatus.SKIPPED)
        and not (j.status == BuildStatus.RUNNING and int(j.id) in live_attempt_jobs)
    ]
    if not reopen:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nothing left to resume (no failed/cancelled/blocked jobs)",
        )
    for job in reopen:
        job.status = BuildStatus.BLOCKED
        job.completed_at = None
        job.blocked_reason = "waiting on repository"
        job.error_message = None
        job.blocking_sources = []
    plan.status = LayerPlanStatus.RUNNING
    plan.error_message = None
    await db.flush()
    await log_audit(
        db, "build.layer_resume", account_id=user.id, resource_type="layer_plan",
        resource_id=str(plan.id), details=f"reopened {len(reopen)} job(s)",
        ip_address=get_client_ip(request), user_agent=get_user_agent(request),
    )
    return plan, [int(j.id) for j in reopen]


async def maybe_settle_layer_plan(db: Any, layer_plan_id: int) -> None:
    """Mark a plan terminal once every job it owns has reached a terminal state."""
    plan = await db.get(LayerPlan, layer_plan_id)
    if not plan or plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
        return
    jobs_r = await db.execute(
        select(BuildJob)
        .options(selectinload(BuildJob.package))
        .where(BuildJob.layer_plan_id == plan.id)
    )
    jobs = list(jobs_r.scalars().all())
    if not jobs:
        return

    if any(j.status in _IN_FLIGHT_STATUSES for j in jobs):
        if plan.status == LayerPlanStatus.PENDING and any(
            j.status not in (BuildStatus.PENDING, BuildStatus.BLOCKED) for j in jobs
        ):
            plan.status = LayerPlanStatus.RUNNING
        return
    terminal, message = layer_plan_settlement(jobs)
    if terminal is None:
        return
    plan.status = terminal
    plan.error_message = message
    notify_layer_plan_terminal(plan.id, terminal.value, plan.error_message)


async def list_interrupted_layer_planning(db: Any) -> list[int]:
    """Layer plans that never finished seeding after a crash/restart."""
    result = await db.execute(
        select(LayerPlan).where(LayerPlan.status == LayerPlanStatus.PENDING)
    )
    out: list[int] = []
    for plan in result.scalars():
        job_n = await db.scalar(
            select(BuildJob.id).where(BuildJob.layer_plan_id == plan.id).limit(1)
        )
        if job_n is None:
            out.append(int(plan.id))
    return out


async def cancel_interrupted_layer_planning(db: Any) -> list[int]:
    """PENDING plans with zero jobs at startup never finished background seeding."""
    ids = await list_interrupted_layer_planning(db)
    if not ids:
        return []
    result = await db.execute(select(LayerPlan).where(LayerPlan.id.in_(ids)))
    out: list[int] = []
    for plan in result.scalars():
        plan.status = LayerPlanStatus.CANCELLED
        plan.error_message = "Layer plan seeding was interrupted by a service restart. Re-trigger it."
        notify_layer_plan_terminal(plan.id, LayerPlanStatus.CANCELLED.value, plan.error_message)
        out.append(int(plan.id))
    return out


async def notify_layer_job_terminal_after_result(
    build_job_id: int,
    status_str: str,
    error_message: str | None,
    resolve_stage: str | None = None,
    missing_provides: list[str] | None = None,
) -> None:
    st = (status_str or "").lower()
    if st not in ("failed", "cancelled"):
        return
    from oreon_build.db.session import async_session_maker
    from sqlalchemy.orm import selectinload

    reason = (error_message or "").strip() or f"Build {st}"
    stage = str(resolve_stage or "").strip().lower()
    async with async_session_maker() as db:
        job = await db.get(
            BuildJob,
            build_job_id,
            options=[selectinload(BuildJob.package)],
        )
        if job is None or job.layer_plan_id is None:
            return
        pkg_name = job.package.name if job.package else None
        if st == "failed" and stage in ("installdeps", "generate_buildrequires"):
            await on_layer_resolve_failed(
                db, job, reason, package_name=pkg_name, missing_provides=missing_provides,
            )
        elif st == "failed":
            await on_build_job_failed_for_layer(db, job, reason, package_name=pkg_name)
        else:
            await on_user_cancelled_layer_job(db, job)
        await db.commit()
