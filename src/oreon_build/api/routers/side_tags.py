# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from oreon_build.api.deps import CurrentUser, CurrentUserOptional, DbSession
from oreon_build.api.schemas import (
    PaginatedResponse,
    SideTagCreate,
    SideTagDeletePackages,
    SideTagInvite,
    SideTagMemberResponse,
    SideTagPackageRow,
    SideTagPromoteRequest,
    SideTagResponse,
)
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.models import (
    Account,
    BuildJob,
    Package,
    Release,
    SideTag,
    SideTagCreatedFrom,
    SideTagMember,
)
from oreon_build.services.promotion_r2_cleanup import list_release_arches
from oreon_build.services.side_tags import (
    PRIMARY_REPO_BRANCHES,
    bump_side_tag_activity,
    can_access_side_tag,
    create_side_tag,
    get_side_tag,
    delete_side_tag_packages,
    require_side_tag_access,
    scrap_side_tag,
    side_tag_is_busy,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/side-tags", tags=["side-tags"])


async def _to_response(db, tag: SideTag, user: Account | None) -> SideTagResponse:
    busy = await side_tag_is_busy(db, tag.id)
    members = [
        SideTagMemberResponse(account_id=m.account_id, username=m.account.username if m.account else "?")
        for m in (tag.members or [])
        if m.account_id
    ]
    return SideTagResponse(
        id=tag.id,
        release_id=tag.release_id,
        releasename=tag.release.releasename if tag.release else None,
        name=tag.name,
        owner_account_id=tag.owner_account_id,
        owner_username=tag.owner.username if tag.owner else None,
        created_from=tag.created_from.value if hasattr(tag.created_from, "value") else str(tag.created_from),
        status="busy" if busy else "idle",
        created_at=tag.created_at,
        last_activity_at=tag.last_activity_at,
        members=members,
        can_access=can_access_side_tag(user, tag),
    )


@router.get("", response_model=PaginatedResponse[SideTagResponse])
async def list_side_tags(
    db: DbSession,
    user: CurrentUserOptional,
    release_id: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    q = (
        select(SideTag)
        .options(
            selectinload(SideTag.owner),
            selectinload(SideTag.release),
            selectinload(SideTag.members).selectinload(SideTagMember.account),
        )
        .order_by(SideTag.last_activity_at.desc())
    )
    cq = select(func.count(SideTag.id))
    if release_id is not None:
        q = q.where(SideTag.release_id == release_id)
        cq = cq.where(SideTag.release_id == release_id)
    total = (await db.execute(cq)).scalar() or 0
    rows = list((await db.execute(q.limit(limit).offset(offset))).scalars().all())
    items = [await _to_response(db, t, user) for t in rows]
    return PaginatedResponse(items=items, total=total, offset=offset)


@router.post("", response_model=SideTagResponse, status_code=status.HTTP_201_CREATED)
async def create_side_tag_endpoint(
    data: SideTagCreate,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    rel = (
        await db.execute(select(Release).where(Release.id == data.release_id))
    ).scalar_one_or_none()
    if not rel:
        raise HTTPException(status_code=404, detail="Release not found")
    if not rel.is_active:
        raise HTTPException(status_code=409, detail="Release is inactive")
    tag = await create_side_tag(
        db,
        release_id=data.release_id,
        name=data.name,
        owner_account_id=user.id,
        created_from=SideTagCreatedFrom.MANUAL,
    )
    await db.flush()
    tag = await get_side_tag(db, tag.id)
    await log_audit(
        db,
        "side_tag.create",
        account_id=user.id,
        resource_type="side_tag",
        resource_id=str(tag.id),
        details=tag.name,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    return await _to_response(db, tag, user)


@router.get("/packages", response_model=PaginatedResponse[SideTagPackageRow])
async def list_packages_across_side_tags(
    db: DbSession,
    release_id: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    search: str | None = Query(None, max_length=200),
):
    """Combined package index from open side tags (Builds page)."""
    # latest job per (package_id, side_tag_id)
    subq = (
        select(
            BuildJob.package_id.label("package_id"),
            BuildJob.side_tag_id.label("side_tag_id"),
            func.max(BuildJob.id).label("max_job_id"),
        )
        .where(BuildJob.side_tag_id.is_not(None))
        .group_by(BuildJob.package_id, BuildJob.side_tag_id)
        .subquery()
    )
    q = (
        select(BuildJob, Package, SideTag, Release)
        .join(subq, BuildJob.id == subq.c.max_job_id)
        .join(Package, Package.id == BuildJob.package_id)
        .join(SideTag, SideTag.id == BuildJob.side_tag_id)
        .join(Release, Release.id == SideTag.release_id)
        .order_by(Package.name.asc(), SideTag.name.asc())
    )
    if release_id is not None:
        q = q.where(SideTag.release_id == release_id)
    if search and search.strip():
        pat = f"%{search.strip()}%"
        q = q.where(Package.name.ilike(pat))
    rows = list((await db.execute(q)).all())
    total = len(rows)
    page = rows[offset : offset + limit]
    items = [
        SideTagPackageRow(
            package_id=pkg.id,
            package_name=pkg.name,
            side_tag_id=tag.id,
            side_tag_name=tag.name,
            release_id=tag.release_id,
            releasename=rel.releasename,
            latest_job_status=job.status.value if job.status else None,
            latest_job_id=job.id,
            updated_at=job.updated_at or job.created_at,
        )
        for job, pkg, tag, rel in page
    ]
    return PaginatedResponse(items=items, total=total, offset=offset)


@router.get("/{side_tag_id}", response_model=SideTagResponse)
async def get_side_tag_endpoint(
    side_tag_id: int,
    db: DbSession,
    user: CurrentUserOptional,
):
    tag = await get_side_tag(db, side_tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Side tag not found")
    return await _to_response(db, tag, user)


@router.post("/{side_tag_id}/invite", response_model=SideTagResponse)
async def invite_member(
    side_tag_id: int,
    data: SideTagInvite,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    tag = await get_side_tag(db, side_tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Side tag not found")
    require_side_tag_access(user, tag)
    uname = (data.username or "").strip()
    acc = (
        await db.execute(
            select(Account)
            .where(Account.username == uname, Account.is_active.is_(True))
            .options(selectinload(Account.role))
        )
    ).scalar_one_or_none()
    if not acc or not (acc.is_admin or acc.is_maintainer):
        raise HTTPException(status_code=404, detail="Maintainer username not found")
    if acc.id == tag.owner_account_id:
        raise HTTPException(status_code=400, detail="Owner is already on the side tag")
    exists = (
        await db.execute(
            select(SideTagMember.id).where(
                SideTagMember.side_tag_id == tag.id,
                SideTagMember.account_id == acc.id,
            )
        )
    ).scalar_one_or_none()
    if not exists:
        db.add(SideTagMember(side_tag_id=tag.id, account_id=acc.id))
        await bump_side_tag_activity(db, tag.id)
        await log_audit(
            db,
            "side_tag.invite",
            account_id=user.id,
            resource_type="side_tag",
            resource_id=str(tag.id),
            details=f"invited={uname}",
            ip_address=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
        await db.commit()
    tag = await get_side_tag(db, side_tag_id)
    return await _to_response(db, tag, user)


@router.delete("/{side_tag_id}/members/{account_id}", response_model=SideTagResponse)
async def remove_member(
    side_tag_id: int,
    account_id: int,
    db: DbSession,
    user: CurrentUser,
):
    tag = await get_side_tag(db, side_tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Side tag not found")
    require_side_tag_access(user, tag)
    await db.execute(
        delete(SideTagMember).where(
            SideTagMember.side_tag_id == tag.id,
            SideTagMember.account_id == account_id,
        )
    )
    await bump_side_tag_activity(db, tag.id)
    await db.commit()
    tag = await get_side_tag(db, side_tag_id)
    return await _to_response(db, tag, user)


@router.post("/{side_tag_id}/packages/delete")
async def delete_side_tag_packages_endpoint(
    side_tag_id: int,
    data: SideTagDeletePackages,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    tag = await get_side_tag(db, side_tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Side tag not found")
    require_side_tag_access(user, tag)
    releasename = tag.release.releasename if tag.release else ""
    info = await delete_side_tag_packages(db, tag, data.names, releasename=releasename)
    await log_audit(
        db,
        "side_tag.delete_packages",
        account_id=user.id,
        resource_type="side_tag",
        resource_id=str(tag.id),
        details=" ".join(info["names"]),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    return info


@router.post("/{side_tag_id}/scrap")
async def scrap_side_tag_endpoint(
    side_tag_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    tag = await get_side_tag(db, side_tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Side tag not found")
    require_side_tag_access(user, tag)
    releasename = tag.release.releasename if tag.release else ""
    info = await scrap_side_tag(db, tag, releasename=releasename)
    await log_audit(
        db,
        "side_tag.scrap",
        account_id=user.id,
        resource_type="side_tag",
        resource_id=str(info["id"]),
        details=info["name"],
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    return {"status": "ok", **info}


@router.post("/{side_tag_id}/promote", status_code=status.HTTP_202_ACCEPTED)
async def promote_side_tag_endpoint(
    side_tag_id: int,
    data: SideTagPromoteRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from oreon_build.services.promotion_jobs import enqueue_side_tag_promote

    tag = await get_side_tag(db, side_tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Side tag not found")
    to_branch = (data.to_branch or "").strip().lower()
    if to_branch not in PRIMARY_REPO_BRANCHES:
        raise HTTPException(status_code=400, detail="to_branch must be dev, testing, or stable")
    releasename = tag.release.releasename if tag.release else ""
    arches = list_release_arches(tag.release.architectures if tag.release else None)
    try:
        job = await enqueue_side_tag_promote(
            side_tag_id=tag.id,
            releasename=releasename,
            from_branch=tag.name,
            to_branch=to_branch,
            arches=arches,
            account_id=user.id,
            client_ip=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {
        "status": "accepted",
        "job_id": job["id"],
        "promoted_to": to_branch,
        "side_tag_name": tag.name,
        "message": (
            f"Promote {tag.name} → {to_branch} queued. Tag will be deleted when copy finishes. "
            f"Poll /api/promotions/jobs/{job['id']} for status."
        ),
        "warning": "Side tag will be deleted after promotion completes",
    }
