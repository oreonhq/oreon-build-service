"""Schedules - list public; create/update/delete require admin."""
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from oreon_build.api.deps import CurrentAdmin, DbSession
from oreon_build.api.schemas import ScheduleCreate, ScheduleResponse, ScheduleUpdate
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.models import Release, Schedule

router = APIRouter(prefix="/schedules", tags=["schedules"])


@router.get("", response_model=list[ScheduleResponse])
async def list_schedules(db: DbSession, release_id: int | None = None):
    q = select(Schedule).order_by(Schedule.release_id, Schedule.name)
    if release_id is not None:
        q = q.where(Schedule.release_id == release_id)
    result = await db.execute(q)
    return list(result.scalars().all())


@router.get("/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(schedule_id: int, db: DbSession):
    result = await db.execute(select(Schedule).where(Schedule.id == schedule_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    return s


@router.post("", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    data: ScheduleCreate,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
):
    result = await db.execute(select(Release).where(Release.id == data.release_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    schedule = Schedule(
        release_id=data.release_id,
        name=data.name,
        kind=data.kind,
        cron_expression=data.cron_expression,
        config=data.config,
        is_enabled=data.is_enabled,
    )
    db.add(schedule)
    await db.flush()
    await log_audit(
        db,
        "schedule.create",
        account_id=admin.id,
        resource_type="schedule",
        resource_id=str(schedule.id),
        details=data.name,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return schedule


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(
    schedule_id: int,
    data: ScheduleUpdate,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
):
    result = await db.execute(select(Schedule).where(Schedule.id == schedule_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    if data.cron_expression is not None:
        s.cron_expression = data.cron_expression
    if data.config is not None:
        s.config = data.config
    if data.is_enabled is not None:
        s.is_enabled = data.is_enabled
    await log_audit(
        db,
        "schedule.update",
        account_id=admin.id,
        resource_type="schedule",
        resource_id=str(schedule_id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return s


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: int,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
):
    result = await db.execute(select(Schedule).where(Schedule.id == schedule_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    await db.delete(s)
    await log_audit(
        db,
        "schedule.delete",
        account_id=admin.id,
        resource_type="schedule",
        resource_id=str(schedule_id),
        details=s.name,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
