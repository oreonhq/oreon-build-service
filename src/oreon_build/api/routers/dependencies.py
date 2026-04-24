"""Dependencies graph and missing dependency diagnostics."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from oreon_build.api.deps import DbSession
from oreon_build.models import Release
from oreon_build.services.dependencies_graph import build_dependencies_graph
from oreon_build.services.distgit_order import compute_distgit_build_order

router = APIRouter(prefix="/dependencies", tags=["dependencies"])


@router.get("/graph", response_model=dict)
async def dependencies_graph(db: DbSession) -> dict:
    try:
        return await build_dependencies_graph(db)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/distgit-build-order", response_model=dict)
async def distgit_build_order(
    db: DbSession,
    release_id: int = Query(..., ge=1, description="Release id (releasename selects distgit path)"),
    distgit_url: str | None = Query(
        None,
        max_length=1024,
        description="Override distgit Git URL (default from settings or oreonhq/rpm-specfiles)",
    ),
    distgit_branch: str | None = Query(None, max_length=128, description="Git branch (default main)"),
) -> dict:
    r = await db.get(Release, release_id)
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    reln = (r.releasename or "").strip()
    if not reln:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Release has no releasename")
    from oreon_build.config import get_settings

    s = get_settings()
    u = (distgit_url or getattr(s, "mass_rebuild_distgit_url", None) or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    b = (distgit_branch or getattr(s, "mass_rebuild_distgit_branch", None) or "main").strip()
    out = await compute_distgit_build_order(releasename=reln, distgit_url=u, distgit_branch=b)
    return {
        "release_id": release_id,
        "releasename": reln,
        "distgit_url": u,
        "distgit_branch": b,
        "package_count": len(out.package_names),
        "package_names": out.package_names,
        "edges": out.edges,
        "layers": out.layers,
        "errors": out.errors,
        "cycle_warning": out.cycle_warning,
    }
