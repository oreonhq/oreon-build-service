"""Releases - list public, create/update/delete require auth (admin)."""
from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from oreon_build.api.deps import CurrentAdmin, DbSession
from oreon_build.api.schemas import (
    PaginatedResponse,
    ReleaseCreate,
    ReleaseRepoResponse,
    ReleaseResponse,
    ReleaseUpdate,
)
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.models import Release, ReleaseChannel, ReleaseRepo

router = APIRouter(prefix="/releases", tags=["releases"])


def _release_to_response(r: Release) -> ReleaseResponse:
    return ReleaseResponse(
        id=r.id,
        releasename=r.releasename,
        description=r.description,
        architectures=r.architectures,
        default_channel=r.default_channel.value,
        is_active=r.is_active,
        created_at=r.created_at,
        base_repos=[
            ReleaseRepoResponse(id=br.id, name=br.name, baseurl=br.baseurl, priority=br.priority, enabled=br.enabled)
            for br in r.base_repos
        ],
    )


@router.get("", response_model=PaginatedResponse[ReleaseResponse])
async def list_releases(
    db: DbSession,
    limit: int = Query(25, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    total = (await db.execute(select(func.count()).select_from(Release))).scalar() or 0
    result = await db.execute(
        select(Release)
        .options(selectinload(Release.base_repos))
        .order_by(Release.releasename)
        .limit(limit)
        .offset(offset)
    )
    releases = result.scalars().all()
    return PaginatedResponse(items=[_release_to_response(r) for r in releases], total=total)


@router.get("/{release_id}", response_model=ReleaseResponse)
async def get_release(release_id: int, db: DbSession):
    result = await db.execute(
        select(Release).where(Release.id == release_id).options(selectinload(Release.base_repos))
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    return _release_to_response(r)


@router.post("", response_model=ReleaseResponse, status_code=status.HTTP_201_CREATED)
async def create_release(
    data: ReleaseCreate,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
):
    result = await db.execute(select(Release).where(Release.releasename == data.releasename))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Releasename already exists")
    release = Release(
        releasename=data.releasename,
        description=data.description,
        architectures=data.architectures,
        default_channel=ReleaseChannel(data.default_channel.value),
        is_active=True,
    )
    db.add(release)
    await db.flush()
    for br in data.base_repos:
        repo = ReleaseRepo(
            release_id=release.id,
            name=br.name,
            baseurl=br.baseurl,
            priority=br.priority,
            enabled=br.enabled,
        )
        db.add(repo)
    await db.flush()
    await log_audit(
        db,
        "release.create",
        account_id=admin.id,
        resource_type="release",
        resource_id=str(release.id),
        details=release.releasename,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    result = await db.execute(
        select(Release).where(Release.id == release.id).options(selectinload(Release.base_repos))
    )
    release = result.scalar_one()
    return _release_to_response(release)


@router.patch("/{release_id}", response_model=ReleaseResponse)
async def update_release(
    release_id: int,
    data: ReleaseUpdate,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
):
    result = await db.execute(
        select(Release).where(Release.id == release_id).options(selectinload(Release.base_repos))
    )
    release = result.scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    if data.description is not None:
        release.description = data.description
    if data.architectures is not None:
        release.architectures = data.architectures
    if data.default_channel is not None:
        release.default_channel = ReleaseChannel(data.default_channel.value)
    if data.is_active is not None:
        release.is_active = data.is_active
    if data.base_repos is not None:
        for br in release.base_repos:
            await db.delete(br)
        for br in data.base_repos:
            repo = ReleaseRepo(
                release_id=release.id,
                name=br.name,
                baseurl=br.baseurl,
                priority=br.priority,
                enabled=br.enabled,
            )
            db.add(repo)
    await log_audit(
        db,
        "release.update",
        account_id=admin.id,
        resource_type="release",
        resource_id=str(release_id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.refresh(release)
    return _release_to_response(release)


@router.delete("/{release_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_release(
    release_id: int,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
):
    result = await db.execute(select(Release).where(Release.id == release_id))
    release = result.scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    await db.delete(release)
    await log_audit(
        db,
        "release.delete",
        account_id=admin.id,
        resource_type="release",
        resource_id=str(release_id),
        details=release.releasename,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
