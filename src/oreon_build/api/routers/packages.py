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

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import delete, func, or_, select

from oreon_build.api.deps import CurrentAdmin, CurrentUser, DbSession
from oreon_build.api.schemas import PackageCreate, PackageResponse, PackageUpdate, PaginatedResponse
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.repo_branch import default_repo_branch_segment
from oreon_build.models import (
    Artifact,
    BuildAttempt,
    BuildAttemptFinalization,
    BuildJob,
    BuildTarget,
    Package,
    PackageVersion,
    PublishTask,
    Release,
    RpmCompleteLock,
    Source,
)
from oreon_build.services.r2 import get_r2_client, repo_rpms_key, src_r2_key

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
    result = await db.execute(select(Package).where(Package.id == package_id))
    pkg = result.scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")

    # Delete R2 objects for this package (artifacts and sources) before DB deletes
    job_ids_result = await db.execute(select(BuildJob.id).where(BuildJob.package_id == package_id))
    job_ids = [r[0] for r in job_ids_result.all()]
    attempt_ids: list[int] = []
    if job_ids:
        attempt_ids_result = await db.execute(
            select(BuildAttempt.id).where(BuildAttempt.build_job_id.in_(job_ids))
        )
        attempt_ids = [r[0] for r in attempt_ids_result.all()]

    artifact_keys = []
    repo_keys_to_delete = set()  # repo path keys (releasename/branch/arch/RPMS/ and branch/src/)
    if job_ids:
        if attempt_ids:
            art_result = await db.execute(
                select(Artifact.r2_key).where(
                    Artifact.build_attempt_id.in_(attempt_ids),
                    Artifact.r2_key.isnot(None),
                    Artifact.r2_key != "",
                )
            )
            artifact_keys = [r[0] for r in art_result.all()]
            # Also delete from repo layout (worker uploads to releasename/branch/arch/RPMS/ and branch/src/)
            art_with_job = await db.execute(
                select(
                    Artifact.filename,
                    Release.releasename,
                    Release.default_repo_branch,
                    BuildTarget.architecture,
                )
                .join(BuildAttempt, BuildAttempt.id == Artifact.build_attempt_id)
                .join(BuildJob, BuildJob.id == BuildAttempt.build_job_id)
                .join(Release, Release.id == BuildJob.release_id)
                .outerjoin(BuildTarget, BuildTarget.id == BuildJob.target_id)
                .where(BuildJob.package_id == package_id)
            )
            for row in art_with_job.all():
                filename, releasename, drb, arch = row[0], row[1], row[2], row[3] or "x86_64"
                branch = default_repo_branch_segment(drb)
                if filename.endswith(".src.rpm"):
                    repo_keys_to_delete.add(src_r2_key(releasename, branch, filename))
                else:
                    repo_keys_to_delete.add(repo_rpms_key(releasename, branch, arch, filename))
    pv_ids_result = await db.execute(select(PackageVersion.id).where(PackageVersion.package_id == package_id))
    pv_ids = [r[0] for r in pv_ids_result.all()]
    source_keys = []
    if pv_ids:
        src_result = await db.execute(
            select(Source.r2_key).where(
                Source.package_version_id.in_(pv_ids),
                Source.r2_key.isnot(None),
                Source.r2_key != "",
            )
        )
        source_keys = [r[0] for r in src_result.all()]
    all_keys = list(set(artifact_keys + list(repo_keys_to_delete) + source_keys))
    if all_keys:
        try:
            r2 = get_r2_client()
            for key in all_keys:
                try:
                    r2.delete_object(key)
                except Exception as e:
                    logger.warning("Failed to delete R2 object %s: %s", key, e)
            logger.info(
                "Package delete: removed %s keys from R2 (%s artifact, %s repo, %s source)",
                len(all_keys), len(artifact_keys), len(repo_keys_to_delete), len(source_keys),
            )
        except Exception as e:
            logger.exception("R2 client or delete failed during package delete (package_id=%s): %s", package_id, e)

    # Rows that FK to build_jobs/build_attempts must go before attempts and jobs (see migrations 012, 014).
    if job_ids:
        await db.execute(delete(PublishTask).where(PublishTask.build_job_id.in_(job_ids)))
    if attempt_ids:
        await db.execute(
            delete(BuildAttemptFinalization).where(BuildAttemptFinalization.attempt_id.in_(attempt_ids))
        )
        await db.execute(delete(RpmCompleteLock).where(RpmCompleteLock.attempt_id.in_(attempt_ids)))

    # Delete dependent rows explicitly to satisfy NOT NULL/FK constraints
    # 1) build_attempts -> build_jobs (artifacts are deleted via cascade or we must delete them first)
    await db.execute(
        delete(Artifact).where(
            Artifact.build_attempt_id.in_(
                select(BuildAttempt.id).where(
                    BuildAttempt.build_job_id.in_(
                        select(BuildJob.id).where(BuildJob.package_id == package_id)
                    )
                )
            )
        )
    )
    await db.execute(
        delete(BuildAttempt).where(
            BuildAttempt.build_job_id.in_(
                select(BuildJob.id).where(BuildJob.package_id == package_id)
            )
        )
    )
    # 2) sources -> package_versions
    await db.execute(
        delete(Source).where(
            Source.package_version_id.in_(
                select(PackageVersion.id).where(PackageVersion.package_id == package_id)
            )
        )
    )
    # 3) build_jobs -> packages
    await db.execute(delete(BuildJob).where(BuildJob.package_id == package_id))
    # 4) package_versions -> packages
    await db.execute(delete(PackageVersion).where(PackageVersion.package_id == package_id))
    await db.delete(pkg)
    await log_audit(
        db,
        "package.delete",
        account_id=user.id,
        resource_type="package",
        resource_id=str(package_id),
        details=pkg.name,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
