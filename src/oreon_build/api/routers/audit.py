"""Audit logs - admin only."""
from fastapi import APIRouter, Query
from sqlalchemy import select

from oreon_build.api.deps import CurrentAdmin, DbSession
from oreon_build.api.schemas import AuditLogResponse
from oreon_build.models import AuditLog

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditLogResponse])
async def list_audit_logs(
    db: DbSession,
    _: CurrentAdmin,
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
    action: str | None = None,
):
    q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    if action:
        q = q.where(AuditLog.action == action)
    result = await db.execute(q)
    return list(result.scalars().all())
