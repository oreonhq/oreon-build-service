"""GitLab push webhook: auto-trigger rebuilds for updated distgit packages."""
from __future__ import annotations

from typing import Any, List

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from oreon_build.api.deps import DbSession
from oreon_build.config import get_settings
from oreon_build.core.audit import log_audit
from oreon_build.models import BuildJob, BuildStatus, BuildTarget, Package
from oreon_build.api.routers.builds import _get_or_create_build_target
from oreon_build.services.gitlab import get_gitlab_client

router = APIRouter(prefix="/gitlab", tags=["gitlab"])


@router.post("/webhook")
async def gitlab_push_webhook(request: Request, db: DbSession) -> dict[str, Any]:
    """Handle GitLab push events. For matching packages, auto-rebuild for previously-built (release, arch) combos."""
    settings = get_settings()
    token = request.headers.get("X-Gitlab-Token")
    if settings.gitlab_webhook_secret and token != settings.gitlab_webhook_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid GitLab token")

    body = await request.json()
    event = request.headers.get("X-Gitlab-Event") or body.get("event_name")
    if event not in ("Push Hook", "push"):
        return {"status": "ignored", "reason": "unsupported_event"}

    project = body.get("project") or {}
    project_id = project.get("id") or body.get("project_id")
    if not project_id:
        return {"status": "ignored", "reason": "no_project_id"}

    # Find packages mapped to this GitLab project
    pkg_result = await db.execute(
        select(Package)
        .where(Package.gitlab_project_id == project_id)
        .options(selectinload(Package.build_jobs).selectinload(BuildJob.build_target))
    )
    packages = list(pkg_result.scalars().all())
    if not packages:
        return {"status": "ignored", "reason": "no_matching_package"}

    triggered: list[int] = []

    for pkg in packages:
        if not pkg.build_jobs:
            # Only rebuild if the package was built before.
            continue
        combos: set[tuple[int, str]] = set()
        for j in pkg.build_jobs:
            if j.target_id and j.build_target:
                combos.add((j.release_id, j.build_target.architecture))
        for release_id, arch in combos:
            target = await _get_or_create_build_target(db, release_id, arch, pkg.preferred_mock_environment_id)
            job = BuildJob(
                package_id=pkg.id,
                release_id=release_id,
                target_id=target.id,
                package_version_id=None,
                status=BuildStatus.PENDING,
                priority=0,
                triggered_by_id=None,
            )
            db.add(job)
            await db.flush()
            triggered.append(job.id)
            await log_audit(
                db,
                action="build.gitlab_auto_trigger",
                account_id=None,
                resource_type="build_job",
                resource_id=str(job.id),
                details=f"package_id={pkg.id} release_id={release_id} arch={arch} project_id={project_id}",
                ip_address=None,
                user_agent="gitlab-webhook",
            )

    return {"status": "ok", "jobs": triggered}


@router.get("/projects", response_model=List[dict])
async def list_group_projects(search: str | None = None) -> List[dict[str, Any]]:
    """List GitLab projects for the configured group (for DistGit UI)."""
    settings = get_settings()
    if not settings.gitlab_group_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="GITLAB_GROUP_ID is not configured")
    client = get_gitlab_client()
    try:
        projects = await client.list_group_projects(settings.gitlab_group_id)
    finally:
        await client.close()
    out: List[dict[str, Any]] = []
    q = (search or "").lower()
    for p in projects:
        name = p.get("name") or ""
        path = p.get("path") or ""
        if q and q not in name.lower() and q not in path.lower():
            continue
        out.append(
            {
                "id": p.get("id"),
                "name": name,
                "path": path,
                "web_url": p.get("web_url"),
                "default_branch": p.get("default_branch"),
            }
        )
    return out


@router.get("/projects/{project_id}/branches", response_model=List[dict])
async def list_project_branches_api(project_id: int) -> List[dict[str, Any]]:
    """List branches for a specific GitLab project."""
    client = get_gitlab_client()
    try:
        branches = await client.list_project_branches(project_id)
    finally:
        await client.close()
    out: List[dict[str, Any]] = []
    for b in branches:
        out.append({"name": b.get("name"), "default": bool(b.get("default"))})
    return out

