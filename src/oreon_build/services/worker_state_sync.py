# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oreon_build.models import BuildAttempt, BuildStatus, Worker, WorkerState

async def sync_worker_state_after_attempt_finished(db: AsyncSession, worker: Worker) -> None:
    r = await db.execute(
        select(BuildAttempt.id)
        .where(
            BuildAttempt.worker_id == worker.id,
            BuildAttempt.status == BuildStatus.RUNNING,
        )
        .order_by(BuildAttempt.id.desc())
        .limit(1)
    )
    aid = r.scalar_one_or_none()
    if aid is not None:
        worker.state = WorkerState.BUSY
        worker.current_build_attempt_id = aid
    else:
        worker.state = WorkerState.IDLE
        worker.current_build_attempt_id = None
    await db.flush()
