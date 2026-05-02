"""Dependencies graph and missing dependency diagnostics."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from oreon_build.api.deps import DbSession
from oreon_build.models import Package, Release
from oreon_build.services.dependencies_graph import build_dependencies_graph
from oreon_build.services.distgit_order import (
    compute_unbuilt_spec_list,
    compute_distgit_build_order,
    compute_spec_tree_dependency_plan,
)

router = APIRouter(prefix="/dependencies", tags=["dependencies"])


@router.get("/graph", response_model=dict)
async def dependencies_graph(
    db: DbSession,
    refresh: bool = Query(False, description="Bypass in-process graph cache and rebuild"),
) -> dict:
    try:
        return await build_dependencies_graph(db, bypass_cache=refresh)
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


@router.get("/spec-tree-resolve", response_model=dict)
async def spec_tree_resolve(
    releasename: str = Query(..., min_length=1, max_length=128, description="Release name folder in rpm-specfiles and R2"),
    repo_branch: str = Query("dev", min_length=1, max_length=64, description="R2 repo branch to compare source RPMs against (dev/testing/stable)"),
    existing_only: bool = Query(
        False,
        description=(
            "If false, chain all never-built sources from spec tree. "
            "If true, only include missing sources required by packages already built in repo_branch."
        ),
    ),
    distgit_url: str | None = Query(
        None,
        max_length=1024,
        description="Override distgit Git URL (default from settings or oreonhq/rpm-specfiles)",
    ),
    distgit_branch: str | None = Query(None, max_length=128, description="Git branch (default main)"),
) -> dict:
    from oreon_build.config import get_settings

    s = get_settings()
    u = (distgit_url or getattr(s, "mass_rebuild_distgit_url", None) or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    b = (distgit_branch or getattr(s, "mass_rebuild_distgit_branch", None) or "main").strip()
    out = await compute_spec_tree_dependency_plan(
        releasename=releasename.strip(),
        repo_branch=repo_branch.strip(),
        existing_only=bool(existing_only),
        distgit_url=u,
        distgit_branch=b,
    )
    return {
        "releasename": out.releasename,
        "repo_branch": out.repo_branch,
        "mode": out.mode,
        "distgit_url": out.distgit_url,
        "distgit_branch": out.distgit_branch,
        "total_spec_packages": out.total_spec_packages,
        "built_sources_count": len(out.built_sources),
        "never_built_count": len(out.never_built_sources),
        "selected_count": len(out.selected_for_chain),
        "needed_missing_count": len(out.required_missing_sources),
        "built_sources": out.built_sources,
        "never_built_sources": out.never_built_sources,
        "selected_for_chain": out.selected_for_chain,
        "required_missing_sources": out.required_missing_sources,
        "edges": out.edges,
        "layers": out.layers,
        "chain_string": out.chain_string,
        "unresolved_requirements": out.unresolved_requirements,
        "errors": out.errors,
        "cycle_warning": out.cycle_warning,
    }


@router.get("/unbuilt-list", response_model=dict)
async def unbuilt_list(
    db: DbSession,
    releasename: str = Query(..., min_length=1, max_length=128, description="Release name folder in rpm-specfiles"),
    distgit_url: str | None = Query(
        None,
        max_length=1024,
        description="Override distgit Git URL (default from settings or oreonhq/rpm-specfiles)",
    ),
    distgit_branch: str | None = Query(None, max_length=128, description="Git branch (default main)"),
) -> dict:
    from oreon_build.config import get_settings

    rows = await db.execute(select(Package.name))
    db_package_names = {(n or "").strip() for n in rows.scalars().all() if (n or "").strip()}

    s = get_settings()
    u = (distgit_url or getattr(s, "mass_rebuild_distgit_url", None) or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    b = (distgit_branch or getattr(s, "mass_rebuild_distgit_branch", None) or "main").strip()
    out = await compute_unbuilt_spec_list(
        releasename=releasename.strip(),
        db_package_names=db_package_names,
        distgit_url=u,
        distgit_branch=b,
    )
    return {
        "releasename": out.releasename,
        "distgit_url": out.distgit_url,
        "distgit_branch": out.distgit_branch,
        "total_spec_packages": out.total_spec_packages,
        "total_db_packages": out.total_db_packages,
        "unbuilt_count": len(out.unbuilt_packages),
        "unbuilt_packages": out.unbuilt_packages,
        "errors": out.errors,
    }
