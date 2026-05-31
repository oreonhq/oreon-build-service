#
# Oreon Build Service - Packages API router
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
#
"""Packages - list public; create/update require maintainer or admin; delete requires admin."""
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select

from oreon_build.api.deps import CurrentAdmin, CurrentUser, DbSession
from oreon_build.api.schemas import (
    DeadPackageItem,
    PackageCreate,
    PackageResponse,
    PackageUpdate,
    PaginatedResponse,
    PurgeDeadPackagesAccepted,
    PurgeDeadPackagesResponse,
)
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.models import Package
from oreon_build.services.package_delete import delete_package_record, find_dead_packages, purge_dead_packages_job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/packages", tags=["packages"])


def _package_search_clause(raw: str):
    """Substring match on name or exact id when the term is all digits."""
    term = raw.strip()
    if not term:
        return None
    esc = term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    pat = f"%{esc}%"
    parts = [Package.name.ilike(pat, escape="\\")]
    if term.isdigit():
        parts.append(Package.id == int(term))
    return or_(*parts)


@router.get("", response_model=PaginatedResponse[PackageResponse])
async def list_packages(
    db: DbSession,
    limit: int = Query(25, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    search: str | None = Query(None, max_length=200, description="Filter by package name or id"),
):
    base = select(Package).order_by(Package.name)
    count_q = select(func.count()).select_from(Package)
    sc = _package_search_clause(search) if search else None
    if sc is not None:
        base = base.where(sc)
        count_q = count_q.where(sc)
    total = (await db.execute(count_q)).scalar() or 0
    result = await db.execute(base.limit(limit).offset(offset))
    return PaginatedResponse(items=list(result.scalars().all()), total=total)


@router.get("/dead", response_model=list[DeadPackageItem])
async def list_dead_packages(db: DbSession, user: CurrentAdmin):
    rows = await find_dead_packages(db)
    return [DeadPackageItem.model_validate(r) for r in rows]


@router.post("/purge-dead", status_code=status.HTTP_202_ACCEPTED, response_model=PurgeDeadPackagesAccepted)
async def purge_dead_packages(
    request: Request,
    db: DbSession,
    user: CurrentAdmin,
    background_tasks: BackgroundTasks,
):
    dead = await find_dead_packages(db)
    if not dead:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No dead packages to purge")
    background_tasks.add_task(
        purge_dead_packages_job,
        user_id=user.id,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return PurgeDeadPackagesAccepted(
        status="accepted",
        message="Purge accepted. Dead packages are being deleted in the background.",
        package_count=len(dead),
    )


@router.get("/{package_id}", response_model=PackageResponse)
async def get_package(package_id: int, db: DbSession):
    result = await db.execute(select(Package).where(Package.id == package_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
    return p


@router.post("", response_model=PackageResponse, status_code=status.HTTP_201_CREATED)
async def create_package(
    data: PackageCreate,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    result = await db.execute(select(Package).where(Package.name == data.name))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Package name already exists")
    pkg = Package(
        name=data.name,
        description=data.description,
        preferred_mock_environment_id=data.preferred_mock_environment_id,
        github_repository_id=data.github_repository_id,
        github_html_url=data.github_html_url,
    )
    db.add(pkg)
    await db.flush()
    await log_audit(
        db,
        "package.create",
        account_id=user.id,
        resource_type="package",
        resource_id=str(pkg.id),
        details=data.name,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return pkg


@router.patch("/{package_id}", response_model=PackageResponse)
async def update_package(
    package_id: int,
    data: PackageUpdate,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    result = await db.execute(select(Package).where(Package.id == package_id))
    pkg = result.scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
    patch_fields = data.model_dump(exclude_unset=True)
    if "description" in patch_fields:
        pkg.description = data.description
    if "preferred_mock_environment_id" in patch_fields:
        pkg.preferred_mock_environment_id = data.preferred_mock_environment_id
    if "github_repository_id" in patch_fields:
        pkg.github_repository_id = data.github_repository_id
    if "github_html_url" in patch_fields:
        pkg.github_html_url = data.github_html_url
    await log_audit(
        db,
        "package.update",
        account_id=user.id,
        resource_type="package",
        resource_id=str(package_id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return pkg


@router.delete("/{package_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_package(
    package_id: int,
    request: Request,
    db: DbSession,
    user: CurrentAdmin,
):
    result = await db.execute(select(Package.id, Package.name).where(Package.id == package_id))
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
    await delete_package_record(
        db,
        package_id=row[0],
        package_name=row[1],
        account_id=user.id,
        request=request,
    )
