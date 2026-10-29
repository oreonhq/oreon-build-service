# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from fastapi import APIRouter, Query
from sqlalchemy import String, cast, or_, select

from oreon_build.api.deps import DbSession
from oreon_build.api.routers.builds import _build_job_search_clause
from oreon_build.models import BuildJob, BuildTarget, Package, Release

router = APIRouter(prefix="/search", tags=["search"])


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


@router.get("")
async def search(
    db: DbSession,
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=50),
):
    term = (q or "").strip()
    if not term:
        return {"packages": [], "builds": [], "releases": []}

    try:
        num = int(term)
    except ValueError:
        num = None

    esc = _escape_like(term)
    pat = f"%{esc}%"
    pkg_cond = Package.name.ilike(pat, escape="\\")
    if num is not None:
        pkg_cond = or_(pkg_cond, Package.id == num)
    pkg_result = await db.execute(
        select(Package.id, Package.name).where(pkg_cond).order_by(Package.name).limit(limit)
    )
    packages = [{"id": r[0], "name": r[1]} for r in pkg_result.all()]

    rel_cond = Release.releasename.ilike(pat, escape="\\")
    if num is not None:
        rel_cond = or_(rel_cond, Release.id == num)
    rel_result = await db.execute(
        select(Release.id, Release.releasename).where(rel_cond).order_by(Release.releasename).limit(limit)
    )
    releases = [{"id": r[0], "name": r[1]} for r in rel_result.all()]

    builds: list[dict] = []
    job_sc = _build_job_search_clause(term)
    if job_sc is not None:
        job_result = await db.execute(
            select(
                BuildJob.id,
                BuildJob.package_id,
                BuildJob.release_id,
                BuildJob.status,
                Package.name,
                Release.releasename,
                BuildTarget.architecture,
            )
            .join(Package, BuildJob.package_id == Package.id)
            .join(Release, BuildJob.release_id == Release.id)
            .outerjoin(BuildTarget, BuildJob.target_id == BuildTarget.id)
            .where(job_sc)
            .order_by(BuildJob.created_at.desc())
            .limit(limit)
        )
        for r in job_result.all():
            st = r[3]
            builds.append(
                {
                    "id": r[0],
                    "package_id": r[1],
                    "release_id": r[2],
                    "status": st.value if hasattr(st, "value") else str(st),
                    "package_name": r[4] or "",
                    "release_name": r[5] or "",
                    "architecture": r[6] or "",
                }
            )

    return {"packages": packages, "builds": builds, "releases": releases}
