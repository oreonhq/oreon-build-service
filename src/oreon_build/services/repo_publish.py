from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, update

from oreon_build.core.repo_branch import job_publish_branch
from oreon_build.db.session import async_session_maker
from oreon_build.models import Artifact, BuildAttempt, BuildJob, BuildTarget, PublishTask, Release
from oreon_build.services.publisher import prefix_publish_lock, rpm_publish_locks, update_repodata_at_prefix
from oreon_build.services.r2 import get_r2_client, repo_r2_prefix, repo_rpms_key, src_r2_key

logger = logging.getLogger(__name__)

_publish_locks: dict[int, asyncio.Lock] = {}
_publish_completed_attempts: set[int] = set()

def _repo_publish_timeout_sec() -> int:
    raw = (os.environ.get("OREON_REPO_PUBLISH_TIMEOUT_SEC") or "2400").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 2400
    return max(120, min(n, 12 * 3600))


def _lock_for(attempt_id: int) -> asyncio.Lock:
    lock = _publish_locks.get(attempt_id)
    if lock is None:
        lock = asyncio.Lock()
        _publish_locks[attempt_id] = lock
    return lock

def _cleanup_old_locks() -> None:
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
    attempt_ids: list[int],
    payload_artifacts: list,
    build_attempt_r2_keys: list[str],
) -> None:
    from oreon_build.api.worker_api import _prune_superseded_repo_objects

    prefix = repo_r2_prefix(releasename, branch, arch)
    filenames = [getattr(artifact, "filename", "") for artifact in payload_artifacts]
    with prefix_publish_lock(prefix), rpm_publish_locks(prefix, filenames):
        r2 = get_r2_client()
        from oreon_build.services.publisher import (
            _ensure_stage_sidecar,
            _persistent_repo_root,
            _stage_container_for_prefix,
            _write_staged_rpm,
            REPO_RPM_CACHE_CONTROL,
        )

        container = _stage_container_for_prefix(prefix)
        _ensure_stage_sidecar(container, prefix)
        rpms_dir = _persistent_repo_root(prefix) / "RPMS"
        rpms_dir.mkdir(parents=True, exist_ok=True)
        staged_binaries: list[str] = []

        for a in payload_artifacts:
            if not a.r2_key or not a.filename:
                continue
            fname = a.filename
            key = a.r2_key
            if fname.endswith(".src.rpm"):
                dest = src_r2_key(releasename, branch, fname)
                if "/builds/" in key:
                    data = r2.get_object(key)
                    if not data:
                        continue
                    r2.put_object(
                        dest, data, content_type="application/x-rpm",
                        cache_control=REPO_RPM_CACHE_CONTROL,
                    )
                continue

            dest = repo_rpms_key(releasename, branch, arch, fname)
            data: bytes | None = None
            if key == dest:
                data = r2.get_object(key)
            elif "/builds/" in key:
                data = r2.get_object(key)
                if data:
                    r2.put_object(
                        dest, data, content_type="application/x-rpm",
                        cache_control=REPO_RPM_CACHE_CONTROL,
                    )
            elif key.endswith(f"/RPMS/{fname}") and f"/{branch}/" in key:
                data = r2.get_object(key)
                if data and key != dest:
                    r2.put_object(
                        dest, data, content_type="application/x-rpm",
                        cache_control=REPO_RPM_CACHE_CONTROL,
                    )
            if not data:
                logger.warning(
                    "repo publish %s: skip artifact with unknown r2_key=%r filename=%r",
                    prefix,
                    key,
                    fname,
                )
                continue
            _write_staged_rpm(rpms_dir / fname, data)
            staged_binaries.append(fname)

        keep = {a.filename for a in payload_artifacts if a.filename}
        pruned = _prune_superseded_repo_objects(
            releasename=releasename,
            branch=branch,
            arch=arch,
            keep_artifact_filenames=keep,
            payload_artifacts=payload_artifacts,
        )
        for fn in pruned:
            if not fn or fn.endswith(".src.rpm"):
                continue
            local = rpms_dir / fn
            if local.is_file():
                try:
                    local.unlink()
                except OSError:
                    pass

        if staged_binaries:
            # keep createrepo --update cache; wiping it makes every publish walk ~all RPMs
            update_repodata_at_prefix(prefix, staged_binaries=staged_binaries)
        else:
            logger.info("repo publish %s: no binary RPMs staged, skipping repodata update", prefix)

        for attempt_id in attempt_ids:
            exact_builds_prefix = f"{releasename}/builds/{attempt_id}/"
            for key in build_attempt_r2_keys:
                if key and key.startswith(exact_builds_prefix):
                    try:
                        r2.delete_object(key)
                    except Exception:
                        pass

@dataclass
class _PrefixPublishBatch:
    releasename: str
    branch: str
    arch: str
    prefix: str
    publish_task_ids: list[int] = field(default_factory=list)
    attempt_ids: list[int] = field(default_factory=list)
    payload_artifacts: list = field(default_factory=list)
    build_attempt_r2_keys: list[str] = field(default_factory=list)

def _group_publish_tasks_by_prefix(batches: list[_PrefixPublishBatch]) -> dict[str, _PrefixPublishBatch]:
    out: dict[str, _PrefixPublishBatch] = {}
    for b in batches:
        cur = out.get(b.prefix)
        if cur is None:
            out[b.prefix] = b
            continue
        cur.publish_task_ids.extend(b.publish_task_ids)
        cur.attempt_ids.extend(b.attempt_ids)
        cur.payload_artifacts.extend(b.payload_artifacts)
        cur.build_attempt_r2_keys.extend(b.build_attempt_r2_keys)
    return out

async def _load_publish_batch(task: PublishTask) -> _PrefixPublishBatch | None:
    from oreon_build.api.worker_api import ArtifactPayload

    async with async_session_maker() as db:
        pt_r = await db.execute(select(PublishTask).where(PublishTask.id == task.id))
        pt = pt_r.scalar_one_or_none()
        if pt and pt.status == "success":
            return None

        att_r = await db.execute(select(BuildAttempt).where(BuildAttempt.id == task.attempt_id))
        att = att_r.scalar_one_or_none()
        if not att:
            logger.warning("repo publish: attempt %s not found", task.attempt_id)
            return None

        job_r = await db.execute(select(BuildJob).where(BuildJob.id == att.build_job_id))
        job = job_r.scalar_one_or_none()
        if not job:
            raise RuntimeError(f"build job not found for attempt {task.attempt_id}")

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

        branch = job_publish_branch(job.branch, release)
        arch = target.architecture
        prefix = repo_r2_prefix(release.releasename, branch, arch)

        art_r = await db.execute(select(Artifact).where(Artifact.build_attempt_id == task.attempt_id))
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

    return _PrefixPublishBatch(
        releasename=release.releasename,
        branch=branch,
        arch=arch,
        prefix=prefix,
        publish_task_ids=[task.id],
        attempt_ids=[task.attempt_id],
        payload_artifacts=payload_artifacts,
        build_attempt_r2_keys=r2_keys,
    )

async def _mark_publish_tasks_running(task_ids: list[int]) -> None:
    if not task_ids:
        return
    now = datetime.now(timezone.utc)
    async with async_session_maker() as db:
        await db.execute(
            update(PublishTask)
            .where(PublishTask.id.in_(task_ids), PublishTask.status == "pending")
            .values(status="running", started_at=now)
        )
        await db.commit()

async def _mark_publish_tasks_success(task_ids: list[int], attempt_ids: list[int]) -> None:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as db:
        if task_ids:
            await db.execute(
                update(PublishTask)
                .where(PublishTask.id.in_(task_ids))
                .values(status="success", finished_at=now, error_message=None)
            )
        await db.commit()
    for attempt_id in attempt_ids:
        _publish_completed_attempts.add(attempt_id)
    _cleanup_old_locks()

async def _mark_publish_tasks_failed(task_ids: list[int], attempt_ids: list[int], err: str) -> None:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as db:
        if task_ids:
            await db.execute(
                update(PublishTask)
                .where(PublishTask.id.in_(task_ids))
                .values(status="failed", finished_at=now, error_message=err[:2000])
            )
        await db.commit()
    for attempt_id in attempt_ids:
        _publish_completed_attempts.add(attempt_id)
    _cleanup_old_locks()

async def _source_and_release_for_attempt(
    attempt_id: int,
) -> tuple[str, str, str | None, int] | None:
    from sqlalchemy import select as _select

    from oreon_build.models import BuildAttempt, BuildJob, Package, Release

    async with async_session_maker() as db:
        row = await db.execute(
            _select(Package.name, Release.releasename, BuildTarget.architecture, BuildJob.id)
            .join(BuildJob, BuildJob.package_id == Package.id)
            .join(BuildAttempt, BuildAttempt.build_job_id == BuildJob.id)
            .join(Release, Release.id == BuildJob.release_id)
            .join(BuildTarget, BuildTarget.id == BuildJob.target_id, isouter=True)
            .where(BuildAttempt.id == attempt_id)
            .limit(1)
        )
        hit = row.first()
        if not hit:
            return None
        return str(hit[0]), str(hit[1] or ""), str(hit[2] or "") or None, int(hit[3])


async def _notify_publish_success(attempt_ids: list[int]) -> None:
    from oreon_build.services.layer_engine import on_package_published
    from oreon_build.services.rpmbuild_settle import on_repo_publish_event_for_rpmbuild

    for attempt_id in attempt_ids:
        try:
            info = await _source_and_release_for_attempt(attempt_id)
            if info:
                await on_package_published(info[0], info[1], info[2], job_id=info[3])
        except Exception:
            logger.exception("layer engine unblock after publish attempt_id=%s", attempt_id)
        try:
            await on_repo_publish_event_for_rpmbuild(attempt_id)
        except Exception:
            logger.exception("rpmbuild settle notify after publish attempt_id=%s", attempt_id)

async def _notify_publish_failed(attempt_ids: list[int], err: str) -> None:
    from sqlalchemy import select as _select

    from oreon_build.models import BuildAttempt, BuildJob
    from oreon_build.services.layer_plan import on_build_job_failed_for_layer

    for attempt_id in attempt_ids:
        try:
            async with async_session_maker() as db:
                job_r = await db.execute(
                    _select(BuildJob)
                    .join(BuildAttempt, BuildAttempt.build_job_id == BuildJob.id)
                    .where(BuildAttempt.id == attempt_id)
                    .limit(1)
                )
                job = job_r.scalar_one_or_none()
                if job is not None:
                    await on_build_job_failed_for_layer(db, job, err or "Repo publish failed")
                    await db.commit()
        except Exception:
            logger.exception("layer engine notify publish failure attempt_id=%s", attempt_id)

async def _run_prefix_publish_batch(batch: _PrefixPublishBatch) -> None:
    await _mark_publish_tasks_running(batch.publish_task_ids)
    logger.info(
        "repo publish start prefix=%s attempts=%s artifacts=%s",
        batch.prefix,
        batch.attempt_ids,
        len(batch.payload_artifacts),
    )
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                process_repo_publish_sync,
                releasename=batch.releasename,
                branch=batch.branch,
                arch=batch.arch,
                attempt_ids=batch.attempt_ids,
                payload_artifacts=batch.payload_artifacts,
                build_attempt_r2_keys=batch.build_attempt_r2_keys,
            ),
            timeout=_repo_publish_timeout_sec(),
        )
    except Exception as e:
        logger.exception("repo publish failed prefix=%s attempts=%s", batch.prefix, batch.attempt_ids)
        await _mark_publish_tasks_failed(batch.publish_task_ids, batch.attempt_ids, str(e))
        await _notify_publish_failed(batch.attempt_ids, str(e))
        raise

    await _mark_publish_tasks_success(batch.publish_task_ids, batch.attempt_ids)
    logger.info("repo publish done prefix=%s attempts=%s", batch.prefix, batch.attempt_ids)
    await _notify_publish_success(batch.attempt_ids)

async def run_repo_publish_tasks_batch(tasks: list[PublishTask]) -> None:
    if not tasks:
        return

    loaded: list[_PrefixPublishBatch] = []
    for task in tasks:
        async with _lock_for(task.attempt_id):
            batch = await _load_publish_batch(task)
        if batch is not None:
            loaded.append(batch)

    if not loaded:
        return

    grouped = list(_group_publish_tasks_by_prefix(loaded).values())
    await asyncio.gather(*(_run_prefix_publish_batch(batch) for batch in grouped))

async def run_repo_publish_for_attempt(
    attempt_id: int,
    *,
    publish_task_id: int | None = None,
) -> None:
    async with async_session_maker() as db:
        if publish_task_id is not None:
            pt_r = await db.execute(select(PublishTask).where(PublishTask.id == publish_task_id))
        else:
            pt_r = await db.execute(select(PublishTask).where(PublishTask.attempt_id == attempt_id))
        pt = pt_r.scalar_one_or_none()
        if pt is None:
            logger.warning("repo publish: task not found attempt_id=%s", attempt_id)
            return
        if pt.status == "success":
            logger.info("repo publish skip attempt_id=%s (already success)", attempt_id)
            return
        task = pt

    await run_repo_publish_tasks_batch([task])
