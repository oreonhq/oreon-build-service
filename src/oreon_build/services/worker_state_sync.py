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

"""Keep worker BUSY/IDLE in sync when multiple attempts can be RUNNING on one worker."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oreon_build.models import BuildAttempt, BuildStatus, Worker, WorkerState


async def sync_worker_state_after_attempt_finished(db: AsyncSession, worker: Worker) -> None:
    """
    One worker host may run several builds in parallel. After any attempt finishes (success,
    failure, cancel, or stale sweep), set BUSY if other RUNNING attempts remain for this worker.
    """
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
