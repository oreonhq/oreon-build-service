"""Workers - list public; enroll (admin); heartbeat/poll (worker token)."""
from datetime import datetime, timezone
import secrets

from fastapi import APIRouter, Header, HTTPException, Query, status
from sqlalchemy import func, select

from oreon_build.api.deps import CurrentAdmin, DbSession
from oreon_build.api.schemas import PaginatedResponse, WorkerEnroll, WorkerResponse
from oreon_build.config import get_settings
from oreon_build.core.security import hash_password, verify_worker_token
from oreon_build.models import Worker, WorkerState

router = APIRouter(prefix="/workers", tags=["workers"])


def _worker_response(w: Worker) -> WorkerResponse:
    return WorkerResponse(
        id=w.id,
        name=w.name,
        state=w.state.value,
        architecture=w.architecture,
        last_seen_at=w.last_seen_at,
        last_heartbeat=w.last_heartbeat,
        enrolled_at=w.enrolled_at,
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
    worker = Worker(
        name=data.name,
        token_hash=token_hash,
        state=WorkerState.OFFLINE,
        architecture=data.architecture,
    )
    db.add(worker)
    await db.flush()
    return {
        "worker": _worker_response(worker),
        "token": worker_token,
    }


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
    settings = get_settings()
    result = await db.execute(select(Worker))
    for w in result.scalars().all():
        if verify_worker_token(x_worker_token, settings.secret_key, w.token_hash):
            return w
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token")
