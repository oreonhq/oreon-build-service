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

"""Mark build attempts/jobs stuck in *running* or *pending* as failed (worker crash, hung mock, full disk, no eligible worker, etc.)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oreon_build.config import Settings
from oreon_build.core.audit import log_audit
from oreon_build.core.machine_arch import sql_arch_match_values
from oreon_build.models import (
    BuildAttempt,
    BuildJob,
    BuildStatus,
    BuildTarget,
    Worker,
    WorkerState,
)
from oreon_build.services.worker_state_sync import sync_worker_state_after_attempt_finished

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = (
    BuildStatus.SUCCESS,
    BuildStatus.FAILED,
    BuildStatus.CANCELLED,
    BuildStatus.SKIPPED,
)


async def reconcile_queue_inconsistencies(db: AsyncSession) -> tuple[int, int, int]:
    """
    Repair controller-side drift that blocks the queue under load:

    - **Worker BUSY** with no RUNNING attempts (missed ``sync_worker_state_after_attempt_finished``,
      partial handler failure, or tooling edits). Those workers still *poll*, but UI and any future
      logic that keys off ``current_build_attempt_id`` are wrong; we reset to IDLE.

    - **Worker.current_build_attempt_id** points at a terminal attempt even though the worker row
      still has other RUNNING attempts. Re-point it at the newest RUNNING attempt so the UI
      and poll-claim logic do not wedge on a ghost id.

    - **BuildJob RUNNING** with no RUNNING attempts (job row out of sync with attempts). Align the
      job to the latest attempt terminal status so pending work is not blocked by a ghost running job.

    - **BuildAttempt RUNNING** but parent BuildJob is already in a terminal state (cancel raced
      ahead, earlier handler crashed mid-transition, etc.). Align the attempt to the job status
      so the worker row never stays BUSY against a phantom.

    Returns ``(workers_fixed, jobs_fixed, attempts_fixed)``.
    """
    now = datetime.now(timezone.utc)
    workers_fixed = 0
    jobs_fixed = 0
    attempts_fixed = 0

    # ---- Workers BUSY with no RUNNING attempts ----
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

    # ---- Workers whose current_build_attempt_id points at a terminal attempt ----
    # These workers still have other RUNNING attempts (else the block above already healed them),
    # so we just re-point current_build_attempt_id at the newest RUNNING row.
    stale_ptr_workers = await db.execute(
        select(Worker).where(
            Worker.current_build_attempt_id.isnot(None),
            exists(
                select(1).select_from(BuildAttempt).where(
                    BuildAttempt.id == Worker.current_build_attempt_id,
                    BuildAttempt.status.in_(_TERMINAL_STATUSES),
                )
            ),
        )
    )
    for w in stale_ptr_workers.scalars().all():
        newest_running = await db.scalar(
            select(BuildAttempt.id)
            .where(
                BuildAttempt.worker_id == w.id,
                BuildAttempt.status == BuildStatus.RUNNING,
            )
            .order_by(BuildAttempt.id.desc())
            .limit(1)
        )
        prev_attempt_id = w.current_build_attempt_id
        w.current_build_attempt_id = newest_running
        if newest_running is None and w.state == WorkerState.BUSY:
            w.state = WorkerState.IDLE
        elif newest_running is not None and w.state != WorkerState.BUSY:
            w.state = WorkerState.BUSY
        await db.flush()
        workers_fixed += 1
        logger.warning(
            "Reconciled worker id=%s current_build_attempt_id %s -> %s (previous pointed to terminal attempt)",
            w.id,
            prev_attempt_id,
            newest_running,
        )

    # ---- BuildJob RUNNING with no RUNNING attempts ----
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

    # ---- BuildAttempt RUNNING but parent job already terminal ----
    # This happens when cancel/timeout handlers changed the job row but the attempt update lost
    # the race (transient error, pod kill, connection drop). The attempt would otherwise keep
    # the assigned worker stuck BUSY until the global runtime cutoff (default 48h).
    running_with_terminal_job = await db.execute(
        select(BuildAttempt)
        .join(BuildJob, BuildAttempt.build_job_id == BuildJob.id)
        .where(
            BuildAttempt.status == BuildStatus.RUNNING,
            BuildJob.status.in_(_TERMINAL_STATUSES),
        )
        .options(selectinload(BuildAttempt.worker))
    )
    for att in running_with_terminal_job.scalars().all():
        job_status = await db.scalar(
            select(BuildJob.status).where(BuildJob.id == att.build_job_id)
        )
        if job_status is None or job_status not in _TERMINAL_STATUSES:
            continue
        new_status = job_status
        if job_status == BuildStatus.SUCCESS:
            # Don't promote an attempt to SUCCESS on its own; the controller's result handler is
            # the source of truth for SUCCESS. Mark FAILED so the orphan is cleared.
            new_status = BuildStatus.FAILED
        prev = (att.error_message or "").strip()
        reason = (
            f"[Controller] Parent build_job id={att.build_job_id} already {job_status.value}; "
            "reconciled orphan RUNNING attempt to match."
        )
        att.status = new_status
        att.finished_at = att.finished_at or now
        att.error_message = (prev + "\n\n" if prev else "") + reason
        if att.signing_status in ("not_started", "running", "uploading"):
            att.signing_status = "failed" if new_status != BuildStatus.SUCCESS else "signed"
        await db.flush()
        w3 = att.worker
        if w3 is not None:
            await sync_worker_state_after_attempt_finished(db, w3)
        attempts_fixed += 1
        logger.warning(
            "Reconciled build_attempt id=%s from RUNNING to %s (job %s was %s)",
            att.id,
            new_status.value,
            att.build_job_id,
            job_status.value,
        )

    if workers_fixed or jobs_fixed or attempts_fixed:
        logger.warning(
            "Queue reconcile: fixed %d worker row(s), %d build job row(s), %d attempt row(s)",
            workers_fixed,
            jobs_fixed,
            attempts_fixed,
        )
        await log_audit(
            db,
            action="queue.reconcile",
            account_id=None,
            resource_type="controller",
            resource_id="queue",
            details=(
                f"workers_fixed={workers_fixed} jobs_fixed={jobs_fixed} "
                f"attempts_fixed={attempts_fixed}"
            ),
            ip_address=None,
            user_agent="controller",
        )

    return workers_fixed, jobs_fixed, attempts_fixed


async def sweep_stale_running_builds(db: AsyncSession, settings: Settings) -> int:
    """
    Fail RUNNING attempts (and their jobs) when any of these hold:
    - the attempt has been running longer than ``build_stale_running_minutes``, or
    - the assigned worker has not heartbeated within ``build_stale_worker_heartbeat_minutes``, or
    - the attempt references a ``worker_id`` that no longer exists (worker row deleted while the
      attempt was RUNNING) and the attempt is older than the heartbeat cutoff.

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
        if cutoff_hb is not None and att.worker_id is not None:
            if w is None:
                # Worker row was deleted (admin action, re-enrollment, DB surgery) while the
                # attempt was mid-flight. Use the same age gate as the missing-heartbeat path
                # so we don't fail an attempt the second a worker row disappears.
                if ref < now - timedelta(minutes=hb_min):
                    reasons.append(
                        f"Assigned worker id={att.worker_id} no longer exists and attempt started "
                        f"more than {hb_min} minutes ago (worker row was deleted or re-enrolled)."
                    )
            elif w.last_heartbeat is None and ref < now - timedelta(minutes=hb_min):
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
        if att.signing_status in ("not_started", "running", "uploading"):
            att.signing_status = "failed"

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


async def _no_eligible_live_worker(
    db: AsyncSession, job: BuildJob, worker_offline_after_seconds: int
) -> bool:
    """
    Return True when no worker matching this job's architecture has heartbeated within
    ``worker_offline_after_seconds``. If ``worker_offline_after_seconds`` is 0, treat any
    IDLE/BUSY worker with matching arch as live (DB state only).
    """
    arch_vals: frozenset[str] = frozenset()
    if job.target_id is not None:
        target_arch = await db.scalar(
            select(BuildTarget.architecture).where(BuildTarget.id == job.target_id)
        )
        if target_arch:
            arch_vals = sql_arch_match_values(target_arch)

    q = select(Worker).where(Worker.state.in_([WorkerState.IDLE, WorkerState.BUSY]))
    if arch_vals:
        # Jobs without a target match any arch; here job.target_id is set so we require a match.
        q = q.where(Worker.architecture.in_(list(arch_vals)))

    if worker_offline_after_seconds and worker_offline_after_seconds > 0:
        hb_cutoff = datetime.now(timezone.utc) - timedelta(seconds=worker_offline_after_seconds)
        q = q.where(Worker.last_heartbeat.isnot(None)).where(Worker.last_heartbeat >= hb_cutoff)

    exists_live = await db.scalar(select(func.count()).select_from(q.subquery()))
    return (exists_live or 0) == 0


async def sweep_stale_pending_builds(db: AsyncSession, settings: Settings) -> int:
    """
    Fail ``PENDING``/``QUEUED`` build jobs that have been waiting far too long to be claimed by a
    worker. Without this, a job targeted at an architecture with no enrolled worker (or whose only
    matching worker was decommissioned) would sit in the queue forever because the main
    running-sweep only looks at RUNNING attempts.

    Behavior:
    - If ``build_stale_pending_minutes`` is 0, the sweep is disabled.
    - If ``build_stale_pending_require_no_eligible_worker`` is True, age alone is not enough: the
      job is only failed when **no** worker matching its architecture has heartbeated recently
      (uses ``worker_offline_after_seconds``). Jobs with no target (match-any-arch) are never
      failed by this policy because any live worker can still claim them.
    - Otherwise jobs are failed purely by age, which is safer when you want a hard ceiling on how
      long a build can sit in the queue.

    Returns the number of jobs updated.
    """
    pend_min = settings.build_stale_pending_minutes
    if pend_min <= 0:
        return 0

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=pend_min)

    result = await db.execute(
        select(BuildJob).where(
            BuildJob.status.in_([BuildStatus.PENDING, BuildStatus.QUEUED]),
            BuildJob.created_at < cutoff,
        )
    )
    jobs = list(result.scalars().all())
    updated = 0

    for job in jobs:
        if settings.build_stale_pending_require_no_eligible_worker:
            if job.target_id is None:
                # No target means any live worker can pick this up; don't time it out by age
                # when the user explicitly asked for the stricter mode.
                continue
            if not await _no_eligible_live_worker(
                db, job, settings.worker_offline_after_seconds
            ):
                continue
            reason = (
                f"[Controller] Job sat in queue for more than {pend_min} minutes and no worker "
                "matching the requested architecture has heartbeated recently. Enroll a worker "
                "for this architecture or retrigger the build after the worker pool recovers."
            )
        else:
            reason = (
                f"[Controller] Job sat in queue for more than {pend_min} minutes without being "
                "claimed (no matching worker picked it up). Check that a worker with the required "
                "architecture is enrolled and heartbeating, then retrigger the build."
            )

        prev_status = job.status.value
        # A pending job never had an attempt, so there is no attempt row to finalize here;
        # updating the job row is enough for the queue to clear and for the UI to show it as
        # failed with an explanatory error. error_message lives on attempts, not jobs; the
        # reason text is emitted through audit so admins can find it in the audit log.
        job.status = BuildStatus.FAILED
        job.completed_at = now
        updated += 1
        await db.flush()
        await log_audit(
            db,
            action="build.stale_pending_timeout",
            account_id=None,
            resource_type="build_job",
            resource_id=str(job.id),
            details=reason[:500],
            ip_address=None,
            user_agent="controller",
        )
        logger.warning(
            "Marked build_job id=%s from %s to FAILED (stuck in queue > %d minutes)",
            job.id,
            prev_status,
            pend_min,
        )

    if updated:
        logger.warning("Marked %d stale PENDING/QUEUED build job(s) as failed", updated)

    return updated
