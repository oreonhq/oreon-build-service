# Oreon Build Service
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Mock environments - list public; create/update/delete require admin."""
from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select

from oreon_build.api.deps import CurrentAdmin, DbSession
from oreon_build.api.schemas import (
    MockEnvironmentCreate,
    MockEnvironmentResponse,
    MockEnvironmentUpdate,
    PaginatedResponse,
)
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.models import MockEnvironment, Release

router = APIRouter(prefix="/mock-environments", tags=["mock-environments"])


@router.get("", response_model=PaginatedResponse[MockEnvironmentResponse])
async def list_mock_environments(
    db: DbSession,
    release_id: int | None = None,
    limit: int = Query(25, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    q = select(MockEnvironment).order_by(MockEnvironment.release_id, MockEnvironment.name)
    count_q = select(func.count()).select_from(MockEnvironment)
    if release_id is not None:
        q = q.where(MockEnvironment.release_id == release_id)
        count_q = count_q.where(MockEnvironment.release_id == release_id)
    total = (await db.execute(count_q)).scalar() or 0
    result = await db.execute(q.limit(limit).offset(offset))
    return PaginatedResponse(items=list(result.scalars().all()), total=total)


@router.get("/{env_id}", response_model=MockEnvironmentResponse)
async def get_mock_environment(env_id: int, db: DbSession):
    result = await db.execute(select(MockEnvironment).where(MockEnvironment.id == env_id))
    env = result.scalar_one_or_none()
    if not env:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mock environment not found")
    return env


@router.post("", response_model=MockEnvironmentResponse, status_code=status.HTTP_201_CREATED)
async def create_mock_environment(
    data: MockEnvironmentCreate,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
):
    result = await db.execute(select(Release).where(Release.id == data.release_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    env = MockEnvironment(
        release_id=data.release_id,
        name=data.name,
        config_name=data.config_name,
        architecture=None,
        config_content=data.config_content,
        additional_repo_urls=list(data.additional_repo_urls),
        is_available=data.is_available,
        priority=data.priority,
    )
    db.add(env)
    await db.flush()
    await log_audit(
        db,
        "mock_env.create",
        account_id=admin.id,
        resource_type="mock_environment",
        resource_id=str(env.id),
        details=data.name,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return env


@router.patch("/{env_id}", response_model=MockEnvironmentResponse)
async def update_mock_environment(
    env_id: int,
    data: MockEnvironmentUpdate,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
):
    result = await db.execute(select(MockEnvironment).where(MockEnvironment.id == env_id))
    env = result.scalar_one_or_none()
    if not env:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mock environment not found")
    if data.name is not None:
        env.name = data.name
    if data.config_name is not None:
        env.config_name = data.config_name
    if data.config_content is not None:
        env.config_content = data.config_content
    if data.is_available is not None:
        env.is_available = data.is_available
    if data.priority is not None:
        env.priority = data.priority
    if data.additional_repo_urls is not None:
        env.additional_repo_urls = list(data.additional_repo_urls)
    await log_audit(
        db,
        "mock_env.update",
        account_id=admin.id,
        resource_type="mock_environment",
        resource_id=str(env_id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return env


@router.delete("/{env_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mock_environment(
    env_id: int,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
):
    result = await db.execute(select(MockEnvironment).where(MockEnvironment.id == env_id))
    env = result.scalar_one_or_none()
    if not env:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mock environment not found")
    await db.delete(env)
    await log_audit(
        db,
        "mock_env.delete",
        account_id=admin.id,
        resource_type="mock_environment",
        resource_id=str(env_id),
        details=env.name,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
