"""Workers - list public; enroll (admin); heartbeat/poll (worker token)."""
from datetime import datetime, timezone
import secrets

from fastapi import APIRouter, Header, HTTPException, Query, status
from sqlalchemy import func, select

from oreon_build.api.deps import CurrentAdmin, DbSession
from oreon_build.api.schemas import PaginatedResponse, WorkerEnroll, WorkerResponse, WorkerUpdate
from oreon_build.config import get_settings
from oreon_build.core.security import hash_password, worker_token_fingerprint
from oreon_build.models import Worker, WorkerState

router = APIRouter(prefix="/workers", tags=["workers"])


def _effective_worker_state(w: Worker) -> str:
    """DB state stays idle/busy when a host dies. Treat stale or missing heartbeat as offline for API/UI.

    Burst workers never show as offline -- their VM may be stopped intentionally.
    They always show as idle when not actively running a job.
    """
    if w.is_burst_worker:
        if w.state == WorkerState.BUSY:
            return WorkerState.BUSY.value
        return WorkerState.IDLE.value

    max_age = get_settings().worker_offline_after_seconds
    if max_age <= 0:
        return w.state.value
    if w.last_heartbeat is None:
        return WorkerState.OFFLINE.value
    now = datetime.now(timezone.utc)
    hb = w.last_heartbeat
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    age_sec = (now - hb).total_seconds()
    if age_sec > max_age:
        return WorkerState.OFFLINE.value
    return w.state.value


def _worker_response(w: Worker) -> WorkerResponse:
    return WorkerResponse(
        id=w.id,
        name=w.name,
        state=_effective_worker_state(w),
        architecture=w.architecture,
        last_seen_at=w.last_seen_at,
        last_heartbeat=w.last_heartbeat,
        enrolled_at=w.enrolled_at,
        is_burst_worker=w.is_burst_worker,
        burst_whitelist=list(w.burst_whitelist or []),
        gcp_instance_name=w.gcp_instance_name,
        gcp_zone=w.gcp_zone,
    )


@router.get("", response_model=PaginatedResponse[WorkerResponse])
async def list_workers(
    db: DbSession,
    limit: int = Query(25, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    total = (await db.execute(select(func.count()).select_from(Worker))).scalar() or 0
    result = await db.execute(
        select(Worker).order_by(Worker.name).limit(limit).offset(offset)
    )
    workers = result.scalars().all()
    return PaginatedResponse(items=[_worker_response(w) for w in workers], total=total)


@router.get("/{worker_id}", response_model=WorkerResponse)
async def get_worker(worker_id: int, db: DbSession):
    result = await db.execute(select(Worker).where(Worker.id == worker_id))
    w = result.scalar_one_or_none()
    if not w:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found")
    return _worker_response(w)


@router.post("/enroll", response_model=dict, status_code=status.HTTP_201_CREATED)
async def enroll_worker(data: WorkerEnroll, db: DbSession, _: CurrentAdmin):
    settings = get_settings()
    if data.enrollment_token != settings.worker_enrollment_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid enrollment token")
    result = await db.execute(select(Worker).where(Worker.name == data.name))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Worker name already enrolled")
    worker_token = secrets.token_urlsafe(32)
    token_hash = hash_password(worker_token)
    whitelist = [str(p).strip() for p in (data.burst_whitelist or []) if str(p).strip()]
    worker = Worker(
        name=data.name,
        token_hash=token_hash,
        token_fingerprint=worker_token_fingerprint(worker_token),
        state=WorkerState.OFFLINE,
        architecture=data.architecture,
        is_burst_worker=data.is_burst_worker,
        burst_whitelist=whitelist,
        gcp_instance_name=data.gcp_instance_name or None,
        gcp_zone=data.gcp_zone or None,
    )
    db.add(worker)
    await db.flush()
    return {
        "worker": _worker_response(worker),
        "token": worker_token,
    }


@router.patch("/{worker_id}", response_model=WorkerResponse)
async def update_worker(worker_id: int, data: WorkerUpdate, db: DbSession, _: CurrentAdmin):
    result = await db.execute(select(Worker).where(Worker.id == worker_id))
    w = result.scalar_one_or_none()
    if not w:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found")
    if data.architecture is not None:
        w.architecture = data.architecture or None
    if data.is_burst_worker is not None:
        w.is_burst_worker = data.is_burst_worker
    if data.burst_whitelist is not None:
        w.burst_whitelist = [str(p).strip() for p in data.burst_whitelist if str(p).strip()]
    if data.gcp_instance_name is not None:
        w.gcp_instance_name = data.gcp_instance_name.strip() or None
    if data.gcp_zone is not None:
        w.gcp_zone = data.gcp_zone.strip() or None
    await db.flush()
    return _worker_response(w)


@router.delete("/{worker_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_worker(worker_id: int, db: DbSession, _: CurrentAdmin):
    result = await db.execute(select(Worker).where(Worker.id == worker_id))
    w = result.scalar_one_or_none()
    if not w:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found")
    await db.delete(w)
    await db.flush()


async def _worker_from_token(
    x_worker_token: str = Header(..., alias="X-Worker-Token"),
    db: DbSession = None,
) -> Worker:
    if db is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No session")
    from oreon_build.services.worker_token_resolve import resolve_worker_for_token

    w = await resolve_worker_for_token(db, x_worker_token)
    if w:
        return w
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token")
