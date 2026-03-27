# Oreon Build Service
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

"""Repository status - read-only; publish/compose may be triggered (auth)."""
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from oreon_build.api.deps import CurrentAdmin, CurrentUser, DbSession
from oreon_build.api.schemas import RepositoryResponse
from oreon_build.models import Release, Repository
from oreon_build.services.publisher import update_repodata_at_prefix
from oreon_build.services.r2 import repo_r2_prefix

router = APIRouter(prefix="/repos", tags=["repos"])


@router.get("", response_model=list[RepositoryResponse])
async def list_repos(db: DbSession, release_id: int | None = None):
    q = select(Repository).order_by(Repository.release_id, Repository.channel, Repository.architecture)
    if release_id is not None:
        q = q.where(Repository.release_id == release_id)
    result = await db.execute(q)
    return list(result.scalars().all())


@router.get("/status", response_model=dict)
async def repo_status(db: DbSession, release_id: int | None = None):
    """Summary of repository compose/publish status per release and channel."""
    q = select(Release).where(Release.is_active == True)
    if release_id is not None:
        q = q.where(Release.id == release_id)
    result = await db.execute(q)
    releases = result.scalars().all()
    out = {}
    for r in releases:
        rresult = await db.execute(
            select(Repository).where(Repository.release_id == r.id)
        )
        repos = rresult.scalars().all()
        out[r.releasename] = [
            {
                "channel": repo.channel,
                "architecture": repo.architecture,
                "r2_prefix": repo.r2_prefix,
                "last_compose_at": repo.last_compose_at.isoformat() if repo.last_compose_at else None,
            }
            for repo in repos
        ]
    return out


@router.get("/{repo_id}", response_model=RepositoryResponse)
async def get_repo(repo_id: int, db: DbSession):
    result = await db.execute(select(Repository).where(Repository.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")
    return repo


@router.post("/publish")
async def trigger_publish(
    db: DbSession,
    _: CurrentUser,
    release_id: int = Query(..., description="Release ID"),
    channel: str = Query(..., description="Channel (dev/testing/stable)"),
    architecture: str = Query(..., description="Architecture (e.g. x86_64)"),
):
    """Request repository compose and upload to R2 (async)."""
    result = await db.execute(select(Release).where(Release.id == release_id))
    release = result.scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    prefix = repo_r2_prefix(release.releasename, channel, architecture)
    result = await db.execute(
        select(Repository).where(
            Repository.release_id == release_id,
            Repository.channel == channel,
            Repository.architecture == architecture,
        )
    )
    repo = result.scalar_one_or_none()
    if not repo:
        repo = Repository(
            release_id=release_id,
            channel=channel,
            architecture=architecture,
            r2_prefix=prefix,
        )
        db.add(repo)
        await db.flush()
    return {"status": "accepted", "r2_prefix": prefix}


class RegenerateMetadataResult(BaseModel):
    status: str
    prefixes: list[str]
    refreshed: list[str]
    skipped_no_rpms: list[str]


@router.post("/regenerate-metadata", response_model=RegenerateMetadataResult)
async def regenerate_metadata_all_releases(_: CurrentAdmin, db: DbSession):
    """
    Re-run createrepo for every R2 prefix we know about (DB repositories plus dev/testing/stable × x86_64/aarch64
    for each active release). Prefixes with no RPMs under RPMS/ are skipped after a quick list.
    """
    prefixes: set[str] = set()
    rrepo = await db.execute(select(Repository.r2_prefix))
    for p in rrepo.scalars().all():
        if p:
            prefixes.add(p)
    rels = await db.execute(select(Release).where(Release.is_active.is_(True)))
    for rel in rels.scalars().all():
        for ch in ("dev", "testing", "stable"):
            for arch in ("x86_64", "aarch64"):
                prefixes.add(repo_r2_prefix(rel.releasename, ch, arch))
    ordered = sorted(prefixes)
    refreshed: list[str] = []
    skipped: list[str] = []
    for prefix in ordered:
        if update_repodata_at_prefix(prefix):
            refreshed.append(prefix)
        else:
            skipped.append(prefix)
    return RegenerateMetadataResult(
        status="ok",
        prefixes=ordered,
        refreshed=refreshed,
        skipped_no_rpms=skipped,
    )
