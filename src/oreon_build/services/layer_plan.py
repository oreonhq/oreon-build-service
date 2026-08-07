from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.models import (
    BuildAttempt,
    BuildJob,
    BuildStatus,
    LayerJobBlocker,
    LayerPlan,
    LayerPlanStatus,
    Package,
    PackageVersion,
    Source,
)
from oreon_build.services.distgit_order import db_safe_spec_evr
from oreon_build.services.ci_callback import notify_layer_plan_terminal

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
    await db.flush()
    package_id = pkg.id
    pv = PackageVersion(package_id=pkg.id, version=npd["version"] or "0.0.1")
    db.add(pv)
    await db.flush()
    package_version_id = pv.id
    if npd.get("source_url"):
        db.add(Source(package_version_id=pv.id, kind="distgit", url=npd["source_url"]))
    await db.flush()
    if triggered_by_id is not None:
        await log_audit(
            db,
            "package.create",
            account_id=triggered_by_id,
            resource_type="package",
            resource_id=str(pkg.id),
            details=pkg.name,
            ip_address=get_client_ip(request) if request is not None else None,
            user_agent=get_user_agent(request) if request is not None else "layer-build",
        )
    return package_id, package_version_id


def _job_build_terminal_ok(status_: BuildStatus) -> bool:
    return status_ in (BuildStatus.SUCCESS, BuildStatus.SKIPPED)


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
    jres = await db.execute(
        select(BuildJob).where(
            BuildJob.layer_plan_id == plan.id,
            BuildJob.status.in_(
                (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.BLOCKED, BuildStatus.RUNNING),
            ),
        )
    )
    note = "Layer plan stopped: " + (message or terminal.value)
    for j in jres.scalars().all():
        await _stop_job_and_running_attempts(db, j, note, as_status=open_job_status)
    notify_layer_plan_terminal(plan.id, terminal.value, plan.error_message)


async def on_build_job_failed_for_layer(
    db: Any,
    job: BuildJob,
    reason: str,
    *,
    package_name: str | None = None,
) -> None:
    """Missing-provides expands. Hard fails soft-block waiters then settle if stuck."""
    if job.layer_plan_id is None:
        return
    from oreon_build.services.layer_engine import (
        cascade_fail_dependents,
        parse_missing_provides_from_text,
        try_autofix_layer_missing_provides,
    )

    try:
        if await try_autofix_layer_missing_provides(
            db, job, reason or "", package_name=package_name
        ):
            return
    except Exception:
        logger.exception(
            "layer autofix crashed job_id=%s layer_plan_id=%s",
            job.id,
            job.layer_plan_id,
        )
        if parse_missing_provides_from_text(reason or ""):
            await maybe_settle_layer_plan(db, job.layer_plan_id)
            return
    plan = await db.get(LayerPlan, job.layer_plan_id, with_for_update=True)
    if plan is None:
        return
    if plan.status in (LayerPlanStatus.SUCCESS, LayerPlanStatus.FAILED, LayerPlanStatus.CANCELLED):
        return
    if job.status == BuildStatus.FAILED:
        job.blocked_reason = (reason or job.blocked_reason or "build failed")[:500]
    pkg = package_name
    if not pkg and job.package is not None:
        pkg = job.package.name
    if not pkg:
        pkg_row = await db.get(Package, job.package_id)
        pkg = pkg_row.name if pkg_row else "package"
    short = (reason or "build failed").strip() or "build failed"
    if len(short) > 240:
        short = short[:237] + "..."
    await cascade_fail_dependents(db, job, pkg, short)
    await maybe_settle_layer_plan(db, job.layer_plan_id)


async def on_user_cancelled_layer_job(db: Any, job: BuildJob) -> None:
    if job.layer_plan_id is None:
        return
    from oreon_build.services.layer_engine import cascade_fail_dependents

    await cascade_fail_dependents(db, job, "build", "Cancelled by user")
    await maybe_settle_layer_plan(db, job.layer_plan_id)


async def cancel_layer_plan_by_user(
    db: Any,
    request: Request,
    user: "Account",
    *,
    layer_plan_id: int,
) -> LayerPlan:
    result = await db.execute(select(LayerPlan).where(LayerPlan.id == layer_plan_id).with_for_update())
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layer plan not found")
    if plan.status in (LayerPlanStatus.SUCCESS, LayerPlanStatus.FAILED, LayerPlanStatus.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Layer plan is already {plan.status.value}",
        )
    await fail_layer_plan(db, plan, "Cancelled by user", terminal=LayerPlanStatus.CANCELLED)
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


async def maybe_settle_layer_plan(db: Any, layer_plan_id: int) -> None:
    """Mark a plan terminal once every job it owns has reached a terminal state."""
    plan = await db.get(LayerPlan, layer_plan_id, with_for_update=True)
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

    open_states = (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.BLOCKED, BuildStatus.RUNNING)
    active_states = (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)
    if any(j.status == BuildStatus.BLOCKED for j in jobs) and not any(
        j.status in active_states for j in jobs
    ):
        from oreon_build.services.layer_engine import (
            finalize_blocked_on_dead_upstreams,
            resolve_layer_blocker_cycles,
        )

        broke = await resolve_layer_blocker_cycles(db, plan.id)
        finalized = await finalize_blocked_on_dead_upstreams(db, plan.id)
        if broke or finalized:
            jobs_r = await db.execute(
                select(BuildJob)
                .options(selectinload(BuildJob.package))
                .where(BuildJob.layer_plan_id == plan.id)
            )
            jobs = list(jobs_r.scalars().all())
            if any(j.status == BuildStatus.BLOCKED for j in jobs) and not any(
                j.status in active_states for j in jobs
            ):
                if await finalize_blocked_on_dead_upstreams(db, plan.id):
                    jobs_r = await db.execute(
                        select(BuildJob)
                        .options(selectinload(BuildJob.package))
                        .where(BuildJob.layer_plan_id == plan.id)
                    )
                    jobs = list(jobs_r.scalars().all())

    if any(j.status in open_states for j in jobs):
        if plan.status == LayerPlanStatus.PENDING and any(
            j.status not in (BuildStatus.PENDING, BuildStatus.BLOCKED) for j in jobs
        ):
            plan.status = LayerPlanStatus.RUNNING
        return
    if all(_job_build_terminal_ok(j.status) for j in jobs):
        plan.status = LayerPlanStatus.SUCCESS
        plan.error_message = None
        notify_layer_plan_terminal(plan.id, LayerPlanStatus.SUCCESS.value, None)
        return

    n_ok = sum(1 for j in jobs if _job_build_terminal_ok(j.status))
    n_fail = sum(1 for j in jobs if j.status == BuildStatus.FAILED)
    n_cancel = sum(1 for j in jobs if j.status == BuildStatus.CANCELLED)
    primary: list[str] = []
    seen: set[str] = set()
    for j in jobs:
        if j.status != BuildStatus.FAILED:
            continue
        name = j.package.name if j.package else f"job-{j.id}"
        if name in seen:
            continue
        reason = (j.blocked_reason or "").strip()
        if reason.startswith("upstream ") or reason.startswith("Layer plan stopped:"):
            continue
        if reason.startswith("deadlock:") or reason.startswith("cycle "):
            continue
        if reason.startswith("waiting on:"):
            continue
        seen.add(name)
        primary.append(name)
    bits = [f"{n_ok} success", f"{n_fail} failed", f"{n_cancel} cancelled"]
    msg = "Layer finished with failures (" + ", ".join(bits) + ")."
    if primary:
        msg += " Primary: " + ", ".join(primary[:12])
        if len(primary) > 12:
            msg += f" (+{len(primary) - 12} more)"
    plan.status = LayerPlanStatus.FAILED
    plan.error_message = msg[:8000]
    notify_layer_plan_terminal(plan.id, LayerPlanStatus.FAILED.value, plan.error_message)


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
    result = await db.execute(select(LayerPlan).where(LayerPlan.id.in_(ids)).with_for_update())
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
) -> None:
    st = (status_str or "").lower()
    if st not in ("failed", "cancelled"):
        return
    from oreon_build.db.session import async_session_maker
    from sqlalchemy.orm import selectinload

    reason = (error_message or "").strip() or f"Build {st}"
    async with async_session_maker() as db:
        job = await db.get(
            BuildJob,
            build_job_id,
            options=[selectinload(BuildJob.package)],
        )
        if job is None or job.layer_plan_id is None:
            return
        pkg_name = job.package.name if job.package else None
        if st == "failed":
            from oreon_build.services.layer_engine import parse_missing_provides_from_text

            # publish blocked early so other sessions dont cascade-cancel waiters
            if parse_missing_provides_from_text(reason) and job.status == BuildStatus.FAILED:
                job.status = BuildStatus.BLOCKED
                job.completed_at = None
                job.blocked_reason = "expanding missing provides"
                await db.commit()
                job = await db.get(
                    BuildJob,
                    build_job_id,
                    options=[selectinload(BuildJob.package)],
                    with_for_update=True,
                )
                if job is None or job.layer_plan_id is None:
                    return
            await on_build_job_failed_for_layer(db, job, reason, package_name=pkg_name)
        else:
            await on_user_cancelled_layer_job(db, job)
        await db.commit()
