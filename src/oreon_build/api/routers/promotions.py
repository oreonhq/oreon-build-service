#
# Oreon Build Service - Promotions API router
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Promotions - list candidates (pending); to-testing/to-stable require auth."""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from oreon_build.api.deps import CurrentUser, DbSession
from oreon_build.api.schemas import PaginatedResponse, PromoteRequest, PromotionResponse
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import Artifact, BuildAttempt, BuildJob, BuildStatus, BuildTarget, Package, Promotion, Release
from oreon_build.services.r2 import get_r2_client, repo_rpms_key, src_r2_key
from oreon_build.services.promotion_r2_cleanup import run_post_promotion_r2_maintenance

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


def _copy_branch_to_branch(
    releasename: str,
    from_branch: str,
    to_branch: str,
    arch: str,
    rpm_filenames: list[str],
) -> tuple[list[str], list[str]]:
    """Copy RPMs from from_branch to to_branch in R2. Returns (dest keys, filenames copied)."""
    r2 = get_r2_client()
    dest_keys: list[str] = []
    copied_filenames: list[str] = []
    for fn in rpm_filenames:
        if fn.endswith(".src.rpm"):
            src_key = src_r2_key(releasename, from_branch, fn)
            dest_key = src_r2_key(releasename, to_branch, fn)
        else:
            src_key = repo_rpms_key(releasename, from_branch, arch, fn)
            dest_key = repo_rpms_key(releasename, to_branch, arch, fn)
        data = r2.get_object(src_key)
        if data:
            r2.put_object(dest_key, data)
            dest_keys.append(dest_key)
            copied_filenames.append(fn)
    return dest_keys, copied_filenames


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
    dest_keys, copied_filenames = _copy_branch_to_branch(releasename, branch, dest_channel, arch, rpm_filenames)
    if dest_keys:
        run_post_promotion_r2_maintenance(
            releasename=releasename,
            from_branch=branch,
            dest_channel=dest_channel,
            arch=arch,
            artifact_filenames=rpm_filenames,
            copied_filenames=copied_filenames,
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
