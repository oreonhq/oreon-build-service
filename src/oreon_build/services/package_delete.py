# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

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
from oreon_build.services.current_build_failures import (
    fetch_current_failure_rows,
    is_spec_not_found_error,
)
from oreon_build.services.r2 import get_r2_client, repo_rpms_key, src_r2_key

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)


async def find_dead_packages(db: Any) -> list[dict[str, Any]]:
    rows = await fetch_current_failure_rows(db)
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        att = row.latest_attempt
        err = att.error_message if att else None
        if not is_spec_not_found_error(err):
            continue
        pid = int(row.job.package_id)
        if pid in seen:
            continue
        seen.add(pid)
        sample = (err or "").strip()
        if len(sample) > 512:
            sample = sample[:509] + "..."
        out.append({"id": pid, "name": row.package_name, "sample_error": sample or None})
    out.sort(key=lambda x: (x["name"] or "").lower())
    return out


async def delete_package_record(
    db: Any,
    *,
    package_id: int,
    package_name: str,
    account_id: int,
    request: "Request | None" = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    ip = ip_address if ip_address is not None else (get_client_ip(request) if request else None)
    ua = user_agent if user_agent is not None else (get_user_agent(request) if request else "api")

    job_ids_result = await db.execute(select(BuildJob.id).where(BuildJob.package_id == package_id))
    job_ids = [r[0] for r in job_ids_result.all()]
    attempt_ids: list[int] = []
    if job_ids:
        attempt_ids_result = await db.execute(
            select(BuildAttempt.id).where(BuildAttempt.build_job_id.in_(job_ids))
        )
        attempt_ids = [r[0] for r in attempt_ids_result.all()]

    artifact_keys = []
    repo_keys_to_delete = set()
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
        except Exception as e:
            logger.exception("R2 client or delete failed during package delete (package_id=%s): %s", package_id, e)

    if job_ids:
        await db.execute(delete(PublishTask).where(PublishTask.build_job_id.in_(job_ids)))
    if attempt_ids:
        await db.execute(
            delete(BuildAttemptFinalization).where(BuildAttemptFinalization.attempt_id.in_(attempt_ids))
        )
        await db.execute(delete(RpmCompleteLock).where(RpmCompleteLock.attempt_id.in_(attempt_ids)))

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
            BuildAttempt.build_job_id.in_(select(BuildJob.id).where(BuildJob.package_id == package_id))
        )
    )
    await db.execute(
        delete(Source).where(
            Source.package_version_id.in_(
                select(PackageVersion.id).where(PackageVersion.package_id == package_id)
            )
        )
    )
    await db.execute(delete(BuildJob).where(BuildJob.package_id == package_id))
    await db.execute(delete(PackageVersion).where(PackageVersion.package_id == package_id))
    await db.execute(delete(Package).where(Package.id == package_id))
    await log_audit(
        db,
        "package.delete",
        account_id=account_id,
        resource_type="package",
        resource_id=str(package_id),
        details=package_name,
        ip_address=ip,
        user_agent=ua,
    )


async def purge_dead_packages_job(
    *,
    user_id: int,
    ip_address: str | None,
    user_agent: str | None,
) -> tuple[int, list[str]]:
    from oreon_build.db.session import async_session_maker

    deleted = 0
    failed: list[str] = []
    async with async_session_maker() as db:
        dead = await find_dead_packages(db)
        for row in dead:
            name = row["name"]
            try:
                await delete_package_record(
                    db,
                    package_id=row["id"],
                    package_name=name,
                    account_id=user_id,
                    request=None,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
                await db.commit()
                deleted += 1
            except Exception as e:
                await db.rollback()
                logger.exception("purge dead package_id=%s failed", row["id"])
                failed.append(f"{name}: {e}")
    if deleted or failed:
        logger.info("purge dead finished deleted=%s failed=%s", deleted, len(failed))
    return deleted, failed
