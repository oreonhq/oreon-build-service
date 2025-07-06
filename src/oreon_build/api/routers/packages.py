"""Packages - list public; create/update/delete require maintainer or admin."""
import logging

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import delete, func, select

from oreon_build.api.deps import CurrentUser, DbSession
from oreon_build.api.schemas import PackageCreate, PackageResponse, PackageUpdate, PaginatedResponse
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.models import Artifact, BuildAttempt, BuildJob, BuildTarget, Package, PackageVersion, Release, Source
from oreon_build.services.r2 import get_r2_client, repo_rpms_key, src_r2_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/packages", tags=["packages"])


@router.get("", response_model=PaginatedResponse[PackageResponse])
async def list_packages(
    db: DbSession,
    limit: int = Query(25, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    count_q = select(func.count()).select_from(Package)
    total = (await db.execute(count_q)).scalar() or 0
    result = await db.execute(
        select(Package).order_by(Package.name).limit(limit).offset(offset)
    )
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
        gitlab_project_id=data.gitlab_project_id,
        gitlab_web_url=data.gitlab_web_url,
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
    if data.description is not None:
        pkg.description = data.description
    if "preferred_mock_environment_id" in data.model_dump(exclude_unset=True):
        pkg.preferred_mock_environment_id = data.preferred_mock_environment_id
    if data.gitlab_project_id is not None:
        pkg.gitlab_project_id = data.gitlab_project_id
    if data.gitlab_web_url is not None:
        pkg.gitlab_web_url = data.gitlab_web_url
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
    user: CurrentUser,
):
    result = await db.execute(select(Package).where(Package.id == package_id))
    pkg = result.scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")

    # Delete R2 objects for this package (artifacts and sources) before DB deletes
    job_ids_result = await db.execute(select(BuildJob.id).where(BuildJob.package_id == package_id))
    job_ids = [r[0] for r in job_ids_result.all()]
    artifact_keys = []
    repo_keys_to_delete = set()  # repo path keys (releasename/branch/arch/RPMS/ and branch/src/)
    if job_ids:
        attempt_ids_result = await db.execute(
            select(BuildAttempt.id).where(BuildAttempt.build_job_id.in_(job_ids))
        )
        attempt_ids = [r[0] for r in attempt_ids_result.all()]
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
                select(Artifact.filename, Release.releasename, BuildJob.branch, BuildTarget.architecture)
                .join(BuildAttempt, BuildAttempt.id == Artifact.build_attempt_id)
                .join(BuildJob, BuildJob.id == BuildAttempt.build_job_id)
                .join(Release, Release.id == BuildJob.release_id)
                .outerjoin(BuildTarget, BuildTarget.id == BuildJob.target_id)
                .where(BuildJob.package_id == package_id)
            )
            for row in art_with_job.all():
                filename, releasename, branch, arch = row[0], row[1], row[2] or "dev", row[3] or "x86_64"
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

    # Delete dependent rows explicitly to satisfy NOT NULL / FK constraints
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
