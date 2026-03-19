"""Promotions - list candidates (pending); to-testing / to-stable / keep-dev require auth."""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from oreon_build.api.deps import CurrentUser, DbSession
from oreon_build.api.schemas import PaginatedResponse, PromoteRequest, PromotionResponse
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.models import Artifact, BuildAttempt, BuildJob, BuildStatus, BuildTarget, Package, Promotion, Release
from oreon_build.services.r2 import get_r2_client, repo_r2_prefix, repo_rpms_key
from oreon_build.services.publisher import publish_rpms_to_r2

router = APIRouter(prefix="/promotions", tags=["promotions"])


@router.get("", response_model=PaginatedResponse[PromotionResponse])
async def list_promotions(
    db: DbSession,
    release_id: int | None = None,
    status_filter: str | None = None,
    limit: int = Query(25, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    # Only show promotions whose package still exists. Old promos for deleted packages are hidden.
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


def _copy_branch_to_branch(
    releasename: str,
    from_branch: str,
    to_branch: str,
    arch: str,
    rpm_filenames: list[str],
) -> list[str]:
    """Copy RPMs from from_branch to to_branch in R2; return list of dest keys."""
    r2 = get_r2_client()
    dest_keys = []
    for fn in rpm_filenames:
        src_key = repo_rpms_key(releasename, from_branch, arch, fn)
        dest_key = repo_rpms_key(releasename, to_branch, arch, fn)
        data = r2.get_object(src_key)
        if data:
            r2.put_object(dest_key, data)
            dest_keys.append(dest_key)
    return dest_keys


@router.post("/{promotion_id}/to-testing", response_model=PromotionResponse)
async def promote_to_testing(
    promotion_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
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
    branch = getattr(job, "branch", "dev") or "dev"
    successful = next((a for a in job.attempts if a.status == BuildStatus.SUCCESS), None)
    if not successful or not successful.artifacts:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No successful attempt or artifacts")
    rpm_filenames = [a.filename for a in successful.artifacts if a.filename.endswith(".rpm")]
    dest_keys = _copy_branch_to_branch(releasename, branch, "testing", arch, rpm_filenames)
    if dest_keys:
        existing = get_r2_client().list_objects(repo_r2_prefix(releasename, "testing", arch) + "/RPMS/")
        all_keys = dest_keys + [o["key"] for o in existing if o["key"] not in dest_keys]
        if all_keys:
            publish_rpms_to_r2(repo_r2_prefix(releasename, "testing", arch), all_keys)
    promo.to_channel = "testing"
    promo.status = "promoted_testing"
    promo.promoted_by_id = user.id
    promo.decided_at = datetime.now(timezone.utc)
    await db.flush()
    await log_audit(
        db, "promotion.to_testing", account_id=user.id, resource_type="promotion", resource_id=str(promo.id),
        details=releasename, ip_address=get_client_ip(request), user_agent=get_user_agent(request),
    )
    return promo


@router.post("/{promotion_id}/to-stable", response_model=PromotionResponse)
async def promote_to_stable(
    promotion_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
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
    branch = getattr(job, "branch", "dev") or "dev"
    successful = next((a for a in job.attempts if a.status == BuildStatus.SUCCESS), None)
    if not successful or not successful.artifacts:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No successful attempt or artifacts")
    rpm_filenames = [a.filename for a in successful.artifacts if a.filename.endswith(".rpm")]
    dest_keys = _copy_branch_to_branch(releasename, branch, "stable", arch, rpm_filenames)
    if dest_keys:
        existing = get_r2_client().list_objects(repo_r2_prefix(releasename, "stable", arch) + "/RPMS/")
        all_keys = dest_keys + [o["key"] for o in existing if o["key"] not in dest_keys]
        if all_keys:
            publish_rpms_to_r2(repo_r2_prefix(releasename, "stable", arch), all_keys)
    promo.to_channel = "stable"
    promo.status = "promoted_stable"
    promo.promoted_by_id = user.id
    promo.decided_at = datetime.now(timezone.utc)
    await db.flush()
    await log_audit(
        db, "promotion.to_stable", account_id=user.id, resource_type="promotion", resource_id=str(promo.id),
        details=releasename, ip_address=get_client_ip(request), user_agent=get_user_agent(request),
    )
    return promo


@router.post("/{promotion_id}/keep-dev", response_model=PromotionResponse)
async def keep_in_dev(
    promotion_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    result = await db.execute(select(Promotion).where(Promotion.id == promotion_id))
    promo = result.scalar_one_or_none()
    if not promo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Promotion not found")
    if promo.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promotion already decided")
    promo.status = "kept_dev"
    promo.promoted_by_id = user.id
    promo.decided_at = datetime.now(timezone.utc)
    await db.flush()
    await log_audit(
        db, "promotion.keep_dev", account_id=user.id, resource_type="promotion", resource_id=str(promo.id),
        ip_address=get_client_ip(request), user_agent=get_user_agent(request),
    )
    return promo
