import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from oreon_build.api.deps import CurrentAdmin, CurrentUser, DbSession
from oreon_build.api.schemas import RepositoryResponse
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import Release, Repository
from oreon_build.services.publisher import RepodataDiskQuotaError, update_repodata_at_prefix
from oreon_build.services.r2 import repo_r2_prefix

logger = logging.getLogger(__name__)

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

class RegenerateMetadataFailure(BaseModel):
    prefix: str
    kind: str
    detail: str

class RegenerateMetadataQueued(BaseModel):

    status: str
    message: str
    prefix_count: int

async def _all_repo_prefixes(db: DbSession) -> list[str]:
    prefixes: set[str] = set()
    rrepo = await db.execute(select(Repository.r2_prefix))
    for p in rrepo.scalars().all():
        if p:
            prefixes.add(p)
    rels = await db.execute(select(Release).where(Release.is_active.is_(True)))
    for rel in rels.scalars().all():
        dev_seg = r2_publish_branch(rel)
        for ch in (dev_seg, "testing", "stable"):
            for arch in ("x86_64", "aarch64"):
                prefixes.add(repo_r2_prefix(rel.releasename, ch, arch))
    return sorted(prefixes)

def _regenerate_metadata_job(ordered: list[str], *, force_full: bool = False) -> None:
    refreshed: list[str] = []
    skipped: list[str] = []
    failed: list[RegenerateMetadataFailure] = []
    not_attempted: list[str] = []
    aborted_disk = False
    for prefix in ordered:
        if aborted_disk:
            not_attempted.append(prefix)
            continue
        try:
            if update_repodata_at_prefix(prefix, force_full=force_full):
                refreshed.append(prefix)
            else:
                skipped.append(prefix)
        except RepodataDiskQuotaError as e:
            failed.append(
                RegenerateMetadataFailure(prefix=prefix, kind="disk_quota", detail=str(e))
            )
            aborted_disk = True
        except Exception as e:
            logger.exception("repodata refresh failed for %s", prefix)
            failed.append(
                RegenerateMetadataFailure(prefix=prefix, kind="error", detail=str(e))
            )
    st = "ok"
    if failed or not_attempted:
        st = "partial"
    logger.info(
        "repodata regeneration job finished status=%s force_full=%s refreshed=%d skipped_no_rpms=%d failed=%d not_attempted=%d",
        st,
        force_full,
        len(refreshed),
        len(skipped),
        len(failed),
        len(not_attempted),
    )
    if failed:
        for f in failed[:20]:
            logger.warning("repodata job failure prefix=%s kind=%s detail=%s", f.prefix, f.kind, f.detail)
        if len(failed) > 20:
            logger.warning("repodata job: %d more failures not listed", len(failed) - 20)

async def _run_regenerate_metadata_job(ordered: list[str], *, force_full: bool) -> None:
    await asyncio.to_thread(_regenerate_metadata_job, ordered, force_full=force_full)

def _spawn_regenerate_metadata(ordered: list[str], *, force_full: bool) -> None:
    task = asyncio.create_task(_run_regenerate_metadata_job(ordered, force_full=force_full))

    def _done(t: asyncio.Task[None]) -> None:
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.exception("background repodata regeneration failed", exc_info=exc)

    task.add_done_callback(_done)

@router.post(
    "/regenerate-metadata",
    response_model=RegenerateMetadataQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate_metadata_all_releases(
    _: CurrentAdmin,
    db: DbSession,
):
    ordered = await _all_repo_prefixes(db)
    _spawn_regenerate_metadata(list(ordered), force_full=False)
    return RegenerateMetadataQueued(
        status="queued",
        message="Repodata regeneration is running on the API server. Check oreon-api logs for refreshed counts and errors.",
        prefix_count=len(ordered),
    )

@router.post(
    "/regenerate-metadata-full",
    response_model=RegenerateMetadataQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate_metadata_full_all_releases(
    _: CurrentAdmin,
    db: DbSession,
):
    ordered = await _all_repo_prefixes(db)
    _spawn_regenerate_metadata(list(ordered), force_full=True)
    return RegenerateMetadataQueued(
        status="queued",
        message="Full repodata regeneration is running on the API server. This rescans every RPM and takes longer than a normal refresh. Check oreon-api logs for completion.",
        prefix_count=len(ordered),
    )
