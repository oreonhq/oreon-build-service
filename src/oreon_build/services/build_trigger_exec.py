from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from oreon_build.api.schemas import BuildTrigger
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.models import BuildJob, BuildStatus, MockEnvironment, Package, PackageVersion, Release, Source, Worker
from oreon_build.services.build_target_util import (
    get_or_create_build_target,
    has_successful_build_for_target,
    latest_package_version_id,
    resolve_mock_environment_id,
    resolve_package_by_name,
)
from oreon_build.services.side_tags import bump_side_tag_activity, resolve_required_side_tag

if TYPE_CHECKING:
    from oreon_build.models import Account

logger = logging.getLogger(__name__)

DEFAULT_ARCHITECTURES = ["x86_64", "aarch64"]


def source_url_from_new_package(np) -> str | None:
    if np.source_url:
        return np.source_url
    if np.source_urls:
        return np.source_urls[0]
    return None


def infer_github_html_url_from_new_package(np) -> str | None:
    u = source_url_from_new_package(np)
    if not u or "github.com" not in u.lower():
        return None
    base = u.split("#", 1)[0].strip().rstrip("/")
    if base.lower().endswith(".git"):
        base = base[:-4]
    if not base.lower().startswith("http"):
        return None
    return base


async def _sync_distgit_source(
    db: Any,
    *,
    package_version_id: int,
    source_url: str,
) -> None:
    want = (source_url or "").strip()
    if not want:
        return
    row = await db.execute(
        select(Source)
        .where(
            Source.package_version_id == package_version_id,
            Source.kind == "distgit",
        )
        .limit(1)
    )
    src = row.scalar_one_or_none()
    if src is None:
        db.add(Source(package_version_id=package_version_id, kind="distgit", url=want))
    elif (src.url or "").strip() != want:
        src.url = want
    await db.flush()


async def execute_build_trigger(
    db: Any,
    user: "Account",
    data: BuildTrigger,
    *,
    request: Request | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> list[BuildJob]:
    package_id: int
    package_name: str | None = None
    package_version_id: int | None = data.package_version_id
    preferred_mock_environment_id: int | None = None
    trigger_mock_environment_id: int | None = None
    package_mock_environment_id: int | None = None
    account_id = user.id
    ip = ip_address if ip_address is not None else (get_client_ip(request) if request else None)
    ua = user_agent if user_agent is not None else (get_user_agent(request) if request else "api")

    if data.new_package:
        np = data.new_package
        package_name = np.name
        trigger_mock_environment_id = np.preferred_mock_environment_id
        if trigger_mock_environment_id is not None:
            me_r = await db.execute(
                select(MockEnvironment).where(MockEnvironment.id == trigger_mock_environment_id)
            )
            me = me_r.scalar_one_or_none()
            if not me or me.release_id != data.release_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Mock environment not found or does not belong to the selected release.",
                )
        existing_pkg = await resolve_package_by_name(db, np.name, release_id=data.release_id)
        if existing_pkg:
            package_id = existing_pkg.id
            package_name = existing_pkg.name
            package_mock_environment_id = existing_pkg.preferred_mock_environment_id
        else:
            gh_url = np.github_html_url or infer_github_html_url_from_new_package(np)
            pkg = Package(
                name=np.name,
                description=np.description,
                preferred_mock_environment_id=np.preferred_mock_environment_id,
                github_repository_id=np.github_repository_id,
                github_html_url=gh_url,
            )
            db.add(pkg)
            await db.flush()
            package_id = pkg.id

            pv = PackageVersion(package_id=pkg.id, version=np.version or "0.0.1")
            db.add(pv)
            await db.flush()
            package_version_id = pv.id

            primary_url = source_url_from_new_package(np)
            if primary_url:
                db.add(Source(package_version_id=pv.id, kind=np.source_type.value, url=primary_url))
            for u in (np.source_urls or [])[1:]:
                db.add(Source(package_version_id=pv.id, kind="url", url=u))
            await db.flush()

            await log_audit(
                db,
                "package.create",
                account_id=account_id,
                resource_type="package",
                resource_id=str(pkg.id),
                details=np.name,
                ip_address=ip,
                user_agent=ua,
            )
            package_mock_environment_id = np.preferred_mock_environment_id
    else:
        package_id = data.package_id
        rp = await db.execute(
            select(Package).where(Package.id == package_id).options(selectinload(Package.preferred_mock_environment))
        )
        pkg = rp.scalar_one_or_none()
        if not pkg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
        package_name = pkg.name
        package_mock_environment_id = pkg.preferred_mock_environment_id

    rr = await db.execute(select(Release).where(Release.id == data.release_id))
    release_for_job = rr.scalar_one_or_none()
    if not release_for_job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    side_tag = await resolve_required_side_tag(
        db,
        release_id=data.release_id,
        side_tag_id=getattr(data, "side_tag_id", None),
        user=user,
        require_access=True,
    )
    publish_branch = side_tag.name

    preferred_mock_environment_id = resolve_mock_environment_id(
        trigger_mock_environment_id=trigger_mock_environment_id,
        package_mock_environment_id=package_mock_environment_id,
        release_default_mock_environment_id=release_for_job.default_mock_environment_id,
    )

    if package_version_id is None:
        package_version_id = await latest_package_version_id(db, package_id)

    if data.new_package:
        np = data.new_package
        primary_url = source_url_from_new_package(np)
        if primary_url and np.source_type.value == "distgit":
            if package_version_id is None:
                pv = PackageVersion(package_id=package_id, version=np.version or "0.0.1")
                db.add(pv)
                await db.flush()
                package_version_id = pv.id
            await _sync_distgit_source(db, package_version_id=package_version_id, source_url=primary_url)

    architectures = data.architectures or DEFAULT_ARCHITECTURES
    if not architectures:
        architectures = DEFAULT_ARCHITECTURES

    workers_result = await db.execute(select(Worker))
    workers = list(workers_result.scalars().all())
    workers_by_arch: dict[str, bool] = {}
    for w in workers:
        workers_by_arch[normalize_machine_arch(w.architecture)] = True

    jobs_created: list[BuildJob] = []
    saw_worker_for_requested = False
    for arch in architectures:
        arch = (arch or "").strip()
        if not arch:
            continue
        canon = normalize_machine_arch(arch)
        has_worker = workers_by_arch.get(canon, False)
        if not has_worker:
            continue
        saw_worker_for_requested = True
        target = await get_or_create_build_target(db, data.release_id, canon, preferred_mock_environment_id)
        if data.skip_complete_arches and await has_successful_build_for_target(
            db, package_id, data.release_id, target.id, package_version_id
        ):
            continue
        job = BuildJob(
            package_id=package_id,
            release_id=data.release_id,
            target_id=target.id,
            package_version_id=package_version_id,
            status=BuildStatus.PENDING,
            priority=data.priority,
            triggered_by_id=account_id,
            branch=publish_branch,
            side_tag_id=side_tag.id,
        )
        db.add(job)
        await db.flush()
        jobs_created.append(job)
    if jobs_created:
        await bump_side_tag_activity(db, side_tag.id)

    if not jobs_created:
        req_norm = sorted({normalize_machine_arch(a) for a in architectures if (a or "").strip()})
        avail_norm = sorted(workers_by_arch.keys())
        if not saw_worker_for_requested:
            detail = (
                "No worker enrolled for any selected architecture. "
                f"Requested: {', '.join(req_norm) if req_norm else '(none)'}. "
                f"Workers cover: {', '.join(avail_norm) if avail_norm else '(no workers)'}. "
                "Enroll a worker for at least one selected arch or fix its architecture field."
            )
            logger.warning(
                "build.trigger rejected release_id=%s package=%s package_id=%s %s",
                data.release_id,
                package_name or "?",
                package_id,
                detail,
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
        logger.info(
            "build.trigger skip_all_already_ok release_id=%s package=%s package_id=%s requested=%s skip_complete=%s",
            data.release_id,
            package_name or "?",
            package_id,
            req_norm,
            data.skip_complete_arches,
        )
    for job in jobs_created:
        await log_audit(
            db,
            "build.trigger",
            account_id=account_id,
            resource_type="build_job",
            resource_id=str(job.id),
            details=f"package_id={package_id} release_id={data.release_id}",
            ip_address=ip,
            user_agent=ua,
        )
    return jobs_created
