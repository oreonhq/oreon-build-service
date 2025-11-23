"""Sitewide search: packages, builds, releases."""
from fastapi import APIRouter, Query
from sqlalchemy import or_, select

from oreon_build.api.deps import DbSession
from oreon_build.models import BuildJob, Package, Release

router = APIRouter(prefix="/search", tags=["search"])


@router.get("")
async def search(
    db: DbSession,
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=50),
):
    """Search packages (name, id), builds (job id, package id), releases (name, id)."""
    term = (q or "").strip()
    if not term:
        return {"packages": [], "builds": [], "releases": []}

    # Try numeric for id lookups
    try:
        num = int(term)
    except ValueError:
        num = None

    # Packages: by name (ilike) or id
    pkg_cond = Package.name.ilike(f"%{term}%")
    if num is not None:
        pkg_cond = or_(pkg_cond, Package.id == num)
    pkg_result = await db.execute(
        select(Package.id, Package.name).where(pkg_cond).limit(limit)
    )
    packages = [{"id": r[0], "name": r[1]} for r in pkg_result.all()]

    # Build jobs: by job id or package_id
    job_cond = None
    if num is not None:
        job_cond = or_(BuildJob.id == num, BuildJob.package_id == num)
    if job_cond is None:
        job_result = await db.execute(select(BuildJob.id).limit(0))
    else:
        job_result = await db.execute(
            select(BuildJob.id, BuildJob.package_id).where(job_cond).limit(limit)
        )
    builds = [{"id": r[0], "package_id": r[1] if len(r) > 1 else None} for r in job_result.all()]

    # Releases: by releasename or id
    rel_cond = Release.releasename.ilike(f"%{term}%")
    if num is not None:
        rel_cond = or_(rel_cond, Release.id == num)
    rel_result = await db.execute(
        select(Release.id, Release.releasename).where(rel_cond).limit(limit)
    )
    releases = [{"id": r[0], "name": r[1]} for r in rel_result.all()]

    # If not numeric, also search build jobs by package name (join packages)
    if num is None and term:
        pkg_ids_result = await db.execute(
            select(Package.id).where(Package.name.ilike(f"%{term}%")).limit(limit)
        )
        pkg_ids = [r[0] for r in pkg_ids_result.all()]
        if pkg_ids and len(builds) < limit:
            job_by_pkg = await db.execute(
                select(BuildJob.id, BuildJob.package_id).where(
                    BuildJob.package_id.in_(pkg_ids)
                ).limit(limit - len(builds))
            )
            seen = {b["id"] for b in builds}
            for r in job_by_pkg.all():
                if r[0] not in seen:
                    builds.append({"id": r[0], "package_id": r[1]})
                    seen.add(r[0])

    return {"packages": packages, "builds": builds, "releases": releases}
