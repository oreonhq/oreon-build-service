"""Repository status - read-only; publish/compose may be triggered (auth)."""
from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from oreon_build.api.deps import CurrentUser, DbSession
from oreon_build.api.schemas import RepositoryResponse
from oreon_build.models import Release, Repository

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
    from oreon_build.services.r2 import repo_r2_prefix
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
