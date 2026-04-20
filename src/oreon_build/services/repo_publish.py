# Oreon Build Service
# Copyright (C) 2026 Oreon HQ
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Copy build RPMs into repo layout, prune, regenerate repodata. Used right after POST /worker/result."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import select, update

from oreon_build.config import get_settings
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.db.session import async_session_maker
from oreon_build.models import Artifact, BuildAttempt, BuildJob, BuildTarget, PublishTask, Release
from oreon_build.services.publisher import update_repodata_at_prefix
from oreon_build.services.r2 import get_r2_client, repo_r2_prefix, repo_rpms_key, src_r2_key

logger = logging.getLogger(__name__)

_publish_locks: dict[int, asyncio.Lock] = {}
_publish_global_sem: asyncio.Semaphore | None = None
_publish_global_sem_lock = threading.Lock()
_publish_completed_attempts: set[int] = set()  # Track completed attempts for cleanup


def _repo_publish_timeout_sec() -> int:
    raw = (os.environ.get("OREON_REPO_PUBLISH_TIMEOUT_SEC") or "2400").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 2400
    return max(120, min(n, 12 * 3600))


def _global_publish_semaphore() -> asyncio.Semaphore:
    """One process-wide limit for heavy R2 + createrepo work so burst completions do not stall the API."""
    global _publish_global_sem
    with _publish_global_sem_lock:
        if _publish_global_sem is None:
            n = max(1, int(get_settings().repo_publish_max_concurrent))
            _publish_global_sem = asyncio.Semaphore(n)
            logger.info("repo publish global concurrency limit=%s", n)
        return _publish_global_sem


def _lock_for(attempt_id: int) -> asyncio.Lock:
    lock = _publish_locks.get(attempt_id)
    if lock is None:
        lock = asyncio.Lock()
        _publish_locks[attempt_id] = lock
    return lock


def _cleanup_old_locks() -> None:
    """Remove locks for completed attempts to prevent unbounded memory growth."""
    # Keep only recent completed attempts to avoid memory bloat
    max_cache_size = 100
    if len(_publish_completed_attempts) > max_cache_size:
        oldest = sorted(list(_publish_completed_attempts))[: len(_publish_completed_attempts) - max_cache_size]
        for attempt_id in oldest:
            _publish_locks.pop(attempt_id, None)
            _publish_completed_attempts.discard(attempt_id)


def process_repo_publish_sync(
    *,
    releasename: str,
    branch: str,
    arch: str,
    attempt_id: int,
    payload_artifacts: list,
    build_attempt_r2_keys: list[str],
) -> None:
    from oreon_build.api.worker_api import _prune_superseded_repo_objects

    r2 = get_r2_client()

    for a in payload_artifacts:
        if a.r2_key and "/builds/" in a.r2_key and a.filename:
            data = r2.get_object(a.r2_key)
            if not data:
                continue
            if a.filename.endswith(".src.rpm"):
                dest = src_r2_key(releasename, branch, a.filename)
            else:
                dest = repo_rpms_key(releasename, branch, arch, a.filename)
            r2.put_object(dest, data)

    keep = {a.filename for a in payload_artifacts if a.filename}
    _prune_superseded_repo_objects(
        releasename=releasename,
        branch=branch,
        arch=arch,
        keep_artifact_filenames=keep,
        payload_artifacts=payload_artifacts,
    )

    prefix = repo_r2_prefix(releasename, branch, arch)
    update_repodata_at_prefix(prefix)

    exact_builds_prefix = f"{releasename}/builds/{attempt_id}/"
    for key in build_attempt_r2_keys:
        if key and key.startswith(exact_builds_prefix):
            try:
                r2.delete_object(key)
            except Exception:
                pass


async def run_repo_publish_for_attempt(
    attempt_id: int,
    *,
    publish_task_id: int | None = None,
) -> None:
    """
    Load artifacts for the attempt, run R2 copy/prune/repodata. Serialized per attempt_id.
    If publish_task_id is set (poll loop), claim that row; otherwise claim by attempt_id.
    """
    from oreon_build.api.worker_api import ArtifactPayload

    async with _global_publish_semaphore():
        # Check if already complete - keep lock brief
        async with _lock_for(attempt_id):
            async with async_session_maker() as db:
                pt_r = await db.execute(select(PublishTask).where(PublishTask.attempt_id == attempt_id))
                pt = pt_r.scalar_one_or_none()
                if pt and pt.status == "success":
                    logger.info("repo publish skip attempt_id=%s (already success)", attempt_id)
                    return
        # Lock released here - do heavy work outside lock to avoid starving DB pool

        try:
            async with async_session_maker() as db:
                att_r = await db.execute(select(BuildAttempt).where(BuildAttempt.id == attempt_id))
                att = att_r.scalar_one_or_none()
                if not att:
                    logger.warning("repo publish: attempt %s not found", attempt_id)
                    return

                job_r = await db.execute(select(BuildJob).where(BuildJob.id == att.build_job_id))
                job = job_r.scalar_one_or_none()
                if not job:
                    raise RuntimeError(f"build job not found for attempt {attempt_id}")

                rel_r = await db.execute(select(Release).where(Release.id == job.release_id))
                release = rel_r.scalar_one_or_none()
                if not release:
                    raise RuntimeError(f"release not found for job {job.id}")

                if not job.target_id:
                    raise RuntimeError(f"build target missing for job {job.id}")
                target_r = await db.execute(select(BuildTarget).where(BuildTarget.id == job.target_id))
                target = target_r.scalar_one_or_none()
                if not target or not target.architecture:
                    raise RuntimeError(f"build target arch missing for job {job.id}")

                branch = r2_publish_branch(release)
                arch = target.architecture

                art_r = await db.execute(select(Artifact).where(Artifact.build_attempt_id == attempt_id))
                artifacts = list(art_r.scalars().all())
                payload_artifacts = [
                    ArtifactPayload(
                        kind=a.kind,
                        filename=a.filename,
                        r2_key=a.r2_key,
                        size_bytes=a.size_bytes,
                        checksum_sha256=a.checksum_sha256,
                        signed=bool(a.is_signed),
                    )
                    for a in artifacts
                ]
                r2_keys = [a.r2_key for a in artifacts if a.r2_key]

            now = datetime.now(timezone.utc)
            async with async_session_maker() as db:
                if publish_task_id is not None:
                    await db.execute(
                        update(PublishTask)
                        .where(PublishTask.id == publish_task_id, PublishTask.status == "pending")
                        .values(status="running", started_at=now)
                    )
                else:
                    await db.execute(
                        update(PublishTask)
                        .where(PublishTask.attempt_id == attempt_id, PublishTask.status == "pending")
                        .values(status="running", started_at=now)
                    )
                await db.commit()

            logger.info(
                "repo publish start attempt_id=%s releasename=%s arch=%s artifacts=%s",
                attempt_id,
                release.releasename,
                arch,
                len(payload_artifacts),
            )

            await asyncio.wait_for(
                asyncio.to_thread(
                    process_repo_publish_sync,
                    releasename=release.releasename,
                    branch=branch,
                    arch=arch,
                    attempt_id=attempt_id,
                    payload_artifacts=payload_artifacts,
                    build_attempt_r2_keys=r2_keys,
                ),
                timeout=_repo_publish_timeout_sec(),
            )

            async with async_session_maker() as db:
                await db.execute(
                    update(PublishTask)
                    .where(PublishTask.attempt_id == attempt_id)
                    .values(
                        status="success",
                        finished_at=datetime.now(timezone.utc),
                        error_message=None,
                    )
                )
                await db.commit()
            logger.info("repo publish done attempt_id=%s", attempt_id)
            _publish_completed_attempts.add(attempt_id)
            _cleanup_old_locks()
        except Exception as e:
            logger.exception("repo publish failed attempt_id=%s", attempt_id)
            async with async_session_maker() as db:
                await db.execute(
                    update(PublishTask)
                    .where(PublishTask.attempt_id == attempt_id)
                    .values(
                        status="failed",
                        finished_at=datetime.now(timezone.utc),
                        error_message=str(e)[:2000],
                    )
                )
                await db.commit()
            _publish_completed_attempts.add(attempt_id)
            _cleanup_old_locks()
            raise
