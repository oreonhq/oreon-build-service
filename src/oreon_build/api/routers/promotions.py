from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from oreon_build.api.deps import CurrentUser, DbSession
from oreon_build.api.schemas import BranchPromoteRequest
from oreon_build.core.audit import get_client_ip, get_user_agent
from oreon_build.models import Release
from oreon_build.services.promotion_jobs import enqueue_branch_promote, get_promotion_job
from oreon_build.services.promotion_r2_cleanup import list_release_arches
from oreon_build.services.side_tags import PRIMARY_REPO_BRANCHES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/promotions", tags=["promotions"])


@router.post("/branch", status_code=status.HTTP_202_ACCEPTED)
async def promote_branch_tree(
    data: BranchPromoteRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    """Queue whole-tree R2 promote between primary branches. Poll /promotions/jobs/{id}."""
    from_b = (data.from_branch or "").strip().lower()
    to_b = (data.to_branch or "").strip().lower()
    if from_b not in PRIMARY_REPO_BRANCHES or to_b not in PRIMARY_REPO_BRANCHES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="from_branch and to_branch must be dev, testing, or stable",
        )
    if from_b == to_b:
        raise HTTPException(status_code=400, detail="from_branch and to_branch must differ")
    rel = (
        await db.execute(select(Release).where(Release.id == data.release_id))
    ).scalar_one_or_none()
    if not rel:
        raise HTTPException(status_code=404, detail="Release not found")
    if not rel.is_active:
        raise HTTPException(status_code=409, detail="Release is inactive")
    arches = list_release_arches(rel.architectures)
    try:
        job = await enqueue_branch_promote(
            release_id=rel.id,
            releasename=rel.releasename,
            from_branch=from_b,
            to_branch=to_b,
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
        "releasename": rel.releasename,
        "from_branch": from_b,
        "to_branch": to_b,
        "message": (
            f"Promote {from_b} → {to_b} queued. Source will be emptied after copy finishes. "
            f"Poll /api/promotions/jobs/{job['id']} for status."
        ),
        "warning": f"Source branch '{from_b}' will be emptied when the job completes",
    }


@router.get("/jobs/{job_id}")
async def get_branch_promote_job(job_id: str, user: CurrentUser):
    job = get_promotion_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Promotion job not found")
    return job
