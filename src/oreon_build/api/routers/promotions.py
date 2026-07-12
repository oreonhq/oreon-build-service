from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import selectinload

from oreon_build.api.deps import CurrentUser, DbSession
from oreon_build.api.schemas import (
    BulkPromoteAccepted,
    BulkPromoteRequest,
    PaginatedResponse,
    PromoteRequest,
    PromotionResponse,
)
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import BuildAttempt, BuildJob, BuildStatus, Package, Promotion
from oreon_build.services.promotion_r2_cleanup import (
    copy_branch_to_branch,
    promote_group_from_inventory,
    run_post_promotion_r2_maintenance,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/promotions", tags=["promotions"])


def _promotion_search_clause(raw: str):
    term = raw.strip()
    if not term:
        return None
    esc = term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    pat = f"%{esc}%"
    parts = [
        Package.name.ilike(pat, escape="\\"),
        Promotion.package_name.ilike(pat, escape="\\"),
        Promotion.status.ilike(pat, escape="\\"),
    ]
    if term.isdigit():
        n = int(term)
        parts.extend(
            [
                Promotion.id == n,
                Promotion.package_id == n,
                Promotion.build_job_id == n,
                Promotion.release_id == n,
            ]
        )
    return or_(*parts)


@router.get("", response_model=PaginatedResponse[PromotionResponse])
async def list_promotions(
    db: DbSession,
    release_id: int | None = None,
    status_filter: str | None = None,
    search: str | None = Query(None, max_length=200, description="Filter by package, id, job id, release id, status text"),
    limit: int = Query(25, ge=1),
    offset: int = Query(0, ge=0),
):
    base = (
        select(Promotion)
        .join(Package, Promotion.package_id == Package.id)
        .order_by(Promotion.created_at.desc())
    )
    count_q = select(func.count(Promotion.id)).select_from(Promotion).join(Package, Promotion.package_id == Package.id)
    if release_id is not None:
        base = base.where(Promotion.release_id == release_id)
        count_q = count_q.where(Promotion.release_id == release_id)
    if status_filter is not None:
        base = base.where(Promotion.status == status_filter)
        count_q = count_q.where(Promotion.status == status_filter)
    psc = _promotion_search_clause(search) if search else None
    if psc is not None:
        base = base.where(psc)
        count_q = count_q.where(psc)
    total = (await db.execute(count_q)).scalar() or 0
    result = await db.execute(base.limit(limit).offset(offset))
    return PaginatedResponse(items=list(result.scalars().all()), total=total)


@router.post("/promote", response_model=PromotionResponse, status_code=status.HTTP_201_CREATED)
async def promote(
    data: PromoteRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    if data.from_channel not in ("dev", "testing", "stable") or data.to_channel not in ("dev", "testing", "stable"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid channel")
    promo = Promotion(
        release_id=data.release_id,
        from_channel=data.from_channel,
        to_channel=data.to_channel,
        status="promoted",
        package_name=data.package_name,
        build_job_id=data.build_job_id,
        promoted_by_id=user.id,
        decided_at=datetime.now(timezone.utc),
    )
    db.add(promo)
    await db.flush()
    await log_audit(
        db,
        "promotion.create",
        account_id=user.id,
        resource_type="promotion",
        resource_id=str(promo.id),
        details=f"{data.from_channel} -> {data.to_channel}",
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return promo


async def _promote_pending_to_channel(
    promotion_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    dest_channel: str,
    status_value: str,
    audit_action: str,
) -> Promotion:
    result = await db.execute(
        select(Promotion).where(Promotion.id == promotion_id)
    )
    promo = result.scalar_one_or_none()
    if not promo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Promotion not found")
    if promo.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promotion already decided")
    if not promo.build_job_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No build job for this promotion")
    job_result = await db.execute(
        select(BuildJob).where(BuildJob.id == promo.build_job_id).options(
            selectinload(BuildJob.release),
            selectinload(BuildJob.build_target),
            selectinload(BuildJob.attempts).selectinload(BuildAttempt.artifacts),
        )
    )
    job = job_result.scalar_one_or_none()
    if not job or not job.release or not job.build_target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build job or release/target not found")
    releasename = job.release.releasename
    arch = job.build_target.architecture
    branch = r2_publish_branch(job.release)
    successful = next((a for a in job.attempts if a.status == BuildStatus.SUCCESS), None)
    if not successful or not successful.artifacts:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No successful attempt or artifacts")
    rpm_filenames = [a.filename for a in successful.artifacts if a.filename.endswith(".rpm")]
    if not rpm_filenames:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No RPM artifacts")

    def _do_r2() -> tuple[list[str], list[str]]:
        dest_keys, copied_filenames = copy_branch_to_branch(
            releasename,
            branch,
            dest_channel,
            arch,
            rpm_filenames,
        )
        if dest_keys:
            run_post_promotion_r2_maintenance(
                releasename=releasename,
                from_branch=branch,
                dest_channel=dest_channel,
                arch=arch,
                artifact_filenames=rpm_filenames,
                copied_filenames=copied_filenames,
            )
        return dest_keys, copied_filenames

    dest_keys, _copied = await asyncio.to_thread(_do_r2)
    if not dest_keys:
        promo.status = "missing_artifacts"
        promo.promoted_by_id = user.id
        promo.decided_at = datetime.now(timezone.utc)
        await db.flush()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="RPM objects not present in R2 source or dest branch",
        )
    promo.to_channel = dest_channel
    promo.status = status_value
    promo.promoted_by_id = user.id
    promo.decided_at = datetime.now(timezone.utc)
    await db.flush()
    await log_audit(
        db,
        audit_action,
        account_id=user.id,
        resource_type="promotion",
        resource_id=str(promo.id),
        details=releasename,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return promo


def _spawn_bulk_promote(
    *,
    promotion_ids: list[int],
    dest_channel: str,
    status_value: str,
    audit_action: str,
    user_id: int,
    ip_address: str | None,
    user_agent: str | None,
    from_statuses: tuple[str, ...] = ("pending",),
) -> None:
    task = asyncio.create_task(
        _run_bulk_promote_job(
            promotion_ids=promotion_ids,
            dest_channel=dest_channel,
            status_value=status_value,
            audit_action=audit_action,
            user_id=user_id,
            ip_address=ip_address,
            user_agent=user_agent,
            from_statuses=from_statuses,
        )
    )

    def _done(t: asyncio.Task[None]) -> None:
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.exception("bulk promotion failed", exc_info=exc)

    task.add_done_callback(_done)


async def _run_bulk_promote_job(
    *,
    promotion_ids: list[int],
    dest_channel: str,
    status_value: str,
    audit_action: str,
    user_id: int,
    ip_address: str | None,
    user_agent: str | None,
    from_statuses: tuple[str, ...] = ("pending",),
) -> None:
    from oreon_build.db.session import async_session_maker

    if not promotion_ids:
        return

    groups: dict[tuple[str, str, str, str], list[tuple[int, list[str]]]] = defaultdict(list)
    skipped = 0
    async with async_session_maker() as db:
        result = await db.execute(
            select(Promotion).where(
                Promotion.id.in_(promotion_ids),
                Promotion.status.in_(list(from_statuses)),
            )
        )
        promos = list(result.scalars().all())
        if not promos:
            logger.info("bulk promote: no pending promotions left among %s ids", len(promotion_ids))
            return
        job_ids = [p.build_job_id for p in promos if p.build_job_id]
        jobs_by_id: dict[int, BuildJob] = {}
        if job_ids:
            job_result = await db.execute(
                select(BuildJob)
                .where(BuildJob.id.in_(job_ids))
                .options(
                    selectinload(BuildJob.release),
                    selectinload(BuildJob.build_target),
                    selectinload(BuildJob.attempts).selectinload(BuildAttempt.artifacts),
                )
            )
            jobs_by_id = {j.id: j for j in job_result.scalars().all()}

        for promo in promos:
            if not promo.build_job_id:
                skipped += 1
                continue
            job = jobs_by_id.get(promo.build_job_id)
            if not job or not job.release or not job.build_target:
                skipped += 1
                continue
            successful = next((a for a in job.attempts if a.status == BuildStatus.SUCCESS), None)
            if not successful or not successful.artifacts:
                skipped += 1
                continue
            rpm_filenames = [a.filename for a in successful.artifacts if a.filename.endswith(".rpm")]
            if not rpm_filenames:
                skipped += 1
                continue
            releasename = job.release.releasename
            arch = job.build_target.architecture
            branch = r2_publish_branch(job.release)
            groups[(releasename, branch, dest_channel, arch)].append((promo.id, rpm_filenames))
        await db.commit()

    if not groups:
        logger.info("bulk promote: nothing claimable dest=%s skipped=%s", dest_channel, skipped)
        return

    def _do_r2_groups() -> tuple[list[int], list[int], int]:
        ok_ids: list[int] = []
        missing_ids: list[int] = []
        copied = 0
        for (releasename, branch, dest, arch), items in groups.items():
            try:
                result = promote_group_from_inventory(
                    releasename=releasename,
                    from_branch=branch,
                    dest_channel=dest,
                    arch=arch,
                    items=items,
                )
                ok_ids.extend(result.promoted_ids)
                missing_ids.extend(result.missing_ids)
                copied += result.copied_count
            except Exception:
                logger.exception(
                    "bulk promote R2 group failed releasename=%s arch=%s dest=%s",
                    releasename,
                    arch,
                    dest,
                )
        return ok_ids, missing_ids, copied

    try:
        promoted_ids, missing_ids, copied_count = await asyncio.to_thread(_do_r2_groups)
    except Exception:
        logger.exception("bulk promote R2 work failed")
        raise

    decided_at = datetime.now(timezone.utc)
    status_filter = list(from_statuses)
    async with async_session_maker() as db:
        if promoted_ids:
            for i in range(0, len(promoted_ids), 1000):
                chunk = promoted_ids[i : i + 1000]
                await db.execute(
                    update(Promotion)
                    .where(
                        Promotion.id.in_(chunk),
                        Promotion.status.in_(status_filter),
                    )
                    .values(
                        status=status_value,
                        to_channel=dest_channel,
                        promoted_by_id=user_id,
                        decided_at=decided_at,
                    )
                )
        if missing_ids:
            for i in range(0, len(missing_ids), 1000):
                chunk = missing_ids[i : i + 1000]
                await db.execute(
                    update(Promotion)
                    .where(
                        Promotion.id.in_(chunk),
                        Promotion.status.in_(status_filter),
                    )
                    .values(
                        status="missing_artifacts",
                        to_channel=None,
                        promoted_by_id=user_id,
                        decided_at=decided_at,
                    )
                )
        await log_audit(
            db,
            audit_action,
            account_id=user_id,
            resource_type="promotion",
            resource_id="bulk",
            details=(
                f"bulk {dest_channel} promoted={len(promoted_ids)} "
                f"missing_artifacts={len(missing_ids)} copied_rpms={copied_count} "
                f"skipped={skipped} requested={len(promotion_ids)}"
            ),
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await db.commit()
    logger.info(
        "bulk promote done dest=%s promoted=%s missing_artifacts=%s copied_rpms=%s skipped=%s requested=%s",
        dest_channel,
        len(promoted_ids),
        len(missing_ids),
        copied_count,
        skipped,
        len(promotion_ids),
    )


async def _accept_bulk_promote(
    data: BulkPromoteRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    dest_channel: str,
    status_value: str,
    audit_action: str,
) -> BulkPromoteAccepted:
    if data.all_pending:
        result = await db.execute(
            select(Promotion.id).where(Promotion.status == "pending").order_by(Promotion.id)
        )
        ids = [int(x) for x in result.scalars().all()]
    elif data.promotion_ids:
        ids = sorted({int(x) for x in data.promotion_ids if x is not None})
        if not ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No promotion ids")
        result = await db.execute(
            select(Promotion.id).where(
                Promotion.id.in_(ids),
                Promotion.status == "pending",
            )
        )
        ids = [int(x) for x in result.scalars().all()]
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide promotion_ids or set all_pending=true",
        )
    if not ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No pending promotions to promote")

    _spawn_bulk_promote(
        promotion_ids=ids,
        dest_channel=dest_channel,
        status_value=status_value,
        audit_action=audit_action,
        user_id=user.id,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return BulkPromoteAccepted(
        status="accepted",
        message=(
            f"Promoting {len(ids)} package(s) to {dest_channel} in the background. "
            "Uses an R2 inventory first so only packages that actually exist get copied. "
            "Refresh in a minute or two."
        ),
        promotion_count=len(ids),
    )


@router.post(
    "/bulk/to-testing",
    response_model=BulkPromoteAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def bulk_promote_to_testing(
    data: BulkPromoteRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    return await _accept_bulk_promote(
        data,
        request,
        db,
        user,
        "testing",
        "promoted_testing",
        "promotion.to_testing",
    )


@router.post(
    "/bulk/to-stable",
    response_model=BulkPromoteAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def bulk_promote_to_stable(
    data: BulkPromoteRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    return await _accept_bulk_promote(
        data,
        request,
        db,
        user,
        "stable",
        "promoted_stable",
        "promotion.to_stable",
    )


@router.post(
    "/bulk/recover-missing",
    response_model=BulkPromoteAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def bulk_recover_missing(
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    result = await db.execute(
        select(Promotion.id).where(Promotion.status == "missing_artifacts").order_by(Promotion.id)
    )
    ids = [int(x) for x in result.scalars().all()]
    if not ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No missing_artifacts promotions")
    _spawn_bulk_promote(
        promotion_ids=ids,
        dest_channel="testing",
        status_value="promoted_testing",
        audit_action="promotion.to_testing",
        user_id=user.id,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        from_statuses=("missing_artifacts",),
    )
    return BulkPromoteAccepted(
        status="accepted",
        message=(
            f"Recovering {len(ids)} missing-artifact promotion(s). "
            "Copies whatever binaries still exist on the source branch into testing."
        ),
        promotion_count=len(ids),
    )


@router.post("/{promotion_id}/to-testing", response_model=PromotionResponse)
async def promote_to_testing(
    promotion_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    return await _promote_pending_to_channel(
        promotion_id,
        request,
        db,
        user,
        "testing",
        "promoted_testing",
        "promotion.to_testing",
    )


@router.post("/{promotion_id}/to-stable", response_model=PromotionResponse)
async def promote_to_stable(
    promotion_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    return await _promote_pending_to_channel(
        promotion_id,
        request,
        db,
        user,
        "stable",
        "promoted_stable",
        "promotion.to_stable",
    )
