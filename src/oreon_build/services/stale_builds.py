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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Mark build attempts/jobs stuck in *running* as failed (worker crash, hung mock, full disk, etc.)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oreon_build.config import Settings
from oreon_build.core.audit import log_audit
from oreon_build.models import BuildAttempt, BuildJob, BuildStatus, Worker, WorkerState
from oreon_build.services.worker_state_sync import sync_worker_state_after_attempt_finished

logger = logging.getLogger(__name__)


async def reconcile_queue_inconsistencies(db: AsyncSession) -> tuple[int, int]:
    """
    Repair controller-side drift that blocks the queue under load:

    - **Worker BUSY** with no RUNNING attempts (missed ``sync_worker_state_after_attempt_finished``,
      partial handler failure, or tooling edits). Those workers still *poll*, but UI and any future
      logic that keys off ``current_build_attempt_id`` are wrong; we reset to IDLE.

    - **BuildJob RUNNING** with no RUNNING attempts (job row out of sync with attempts). Align the
    job to the latest attempt terminal status so pending work is not blocked by a ghost running job.

    Returns ``(workers_fixed, jobs_fixed)``.
    """
    now = datetime.now(timezone.utc)
    workers_fixed = 0
    jobs_fixed = 0

    busy_orphan = await db.execute(
        select(Worker).where(
            Worker.state == WorkerState.BUSY,
            ~exists(
                select(1).select_from(BuildAttempt).where(
                    BuildAttempt.worker_id == Worker.id,
                    BuildAttempt.status == BuildStatus.RUNNING,
                )
            ),
        )
    )
    for w in busy_orphan.scalars().all():
        w.state = WorkerState.IDLE
        w.current_build_attempt_id = None
        await db.flush()
        workers_fixed += 1
        logger.warning(
            "Reconciled worker id=%s from BUSY to IDLE (no RUNNING attempts on controller)",
            w.id,
        )

    job_orphan = await db.execute(
        select(BuildJob).where(
            BuildJob.status == BuildStatus.RUNNING,
            ~exists(
                select(1).select_from(BuildAttempt).where(
                    BuildAttempt.build_job_id == BuildJob.id,
                    BuildAttempt.status == BuildStatus.RUNNING,
                )
            ),
        )
    )
    jobs = list(job_orphan.scalars().all())
    for job in jobs:
        last_id = await db.scalar(
            select(func.max(BuildAttempt.id)).where(BuildAttempt.build_job_id == job.id)
        )
        latest: BuildAttempt | None = None
        if last_id is not None:
            latest = await db.get(BuildAttempt, last_id)
        if latest is None:
            job.status = BuildStatus.FAILED
            job.completed_at = now
            jobs_fixed += 1
            logger.warning(
                "Reconciled build_job id=%s from RUNNING to FAILED (no attempts row)",
                job.id,
            )
            continue
        if latest.status == BuildStatus.RUNNING:
            continue
        job.status = latest.status
        job.completed_at = latest.finished_at or now
        jobs_fixed += 1
        logger.warning(
            "Reconciled build_job id=%s from RUNNING to %s (latest attempt id=%s)",
            job.id,
            latest.status.value,
            latest.id,
        )
        w2 = await db.get(Worker, latest.worker_id) if latest.worker_id else None
        if w2 is not None:
            await sync_worker_state_after_attempt_finished(db, w2)

    if workers_fixed or jobs_fixed:
        logger.warning(
            "Queue reconcile: fixed %d worker row(s), %d build job row(s)",
            workers_fixed,
            jobs_fixed,
        )
        await log_audit(
            db,
            action="queue.reconcile",
            account_id=None,
            resource_type="controller",
            resource_id="queue",
            details=f"workers_fixed={workers_fixed} jobs_fixed={jobs_fixed}",
            ip_address=None,
            user_agent="controller",
        )

    return workers_fixed, jobs_fixed


async def sweep_stale_running_builds(db: AsyncSession, settings: Settings) -> int:
    """
    Fail RUNNING attempts (and their jobs) when either:
    - the attempt has been running longer than ``build_stale_running_minutes``, or
    - the assigned worker has not heartbeated within ``build_stale_worker_heartbeat_minutes``.

    Returns the number of attempts updated.
    """
    max_min = settings.build_stale_running_minutes
    hb_min = settings.build_stale_worker_heartbeat_minutes
    if max_min <= 0 and hb_min <= 0:
        return 0

    now = datetime.now(timezone.utc)
    cutoff_run = now - timedelta(minutes=max_min) if max_min > 0 else None
    cutoff_hb = now - timedelta(minutes=hb_min) if hb_min > 0 else None

    result = await db.execute(
        select(BuildAttempt)
        .where(BuildAttempt.status == BuildStatus.RUNNING)
        .options(
            selectinload(BuildAttempt.build_job),
            selectinload(BuildAttempt.worker),
        )
    )
    attempts = list(result.scalars().all())
    updated = 0

    for att in attempts:
        ref = att.started_at or att.created_at
        if ref is None:
            ref = now

        reasons: list[str] = []

        if cutoff_run is not None and ref < cutoff_run:
            reasons.append(
                f"No build result within {max_min} minutes (controller stale-running policy). "
                "Common causes: worker crash, disk full (/tmp or /var/lib/mock), hung mock, or network loss."
            )

        w = att.worker
        if cutoff_hb is not None and att.worker_id is not None and w is not None:
            if w.last_heartbeat is None and ref < now - timedelta(minutes=hb_min):
                reasons.append(
                    f"No worker heartbeat ever recorded and attempt started more than {hb_min} minutes ago "
                    "(controller/worker link may be broken)."
                )
            elif w.last_heartbeat is not None and w.last_heartbeat < cutoff_hb:
                reasons.append(
                    f"Worker last heartbeat older than {hb_min} minutes while build was still marked running "
                    "(worker process likely dead or cannot reach the controller)."
                )

        if not reasons:
            continue

        msg = "[Controller] " + " ".join(reasons)
        att.status = BuildStatus.FAILED
        att.finished_at = now
        prev = (att.error_message or "").strip()
        att.error_message = (prev + "\n\n" if prev else "") + msg

        job: BuildJob | None = att.build_job
        if job is not None and job.status == BuildStatus.RUNNING:
            job.status = BuildStatus.FAILED
            job.completed_at = now

        await db.flush()
        if w is not None:
            await sync_worker_state_after_attempt_finished(db, w)

        updated += 1
        await log_audit(
            db,
            action="build.stale_timeout",
            account_id=None,
            resource_type="build_job",
            resource_id=str(att.build_job_id),
            details=f"attempt_id={att.id} reasons={' | '.join(reasons)[:500]}",
            ip_address=None,
            user_agent="controller",
        )

    if updated:
        logger.warning("Marked %d stale RUNNING build attempt(s) as failed", updated)

    return updated
