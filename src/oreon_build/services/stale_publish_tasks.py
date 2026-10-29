# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from oreon_build.config import Settings
from oreon_build.models import PublishTask

logger = logging.getLogger(__name__)

async def reclaim_publish_tasks_stuck_running(db: AsyncSession, settings: Settings) -> int:
    minutes = int(settings.publish_task_stale_running_minutes or 0)
    if minutes <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    stmt = (
        update(PublishTask)
        .where(
            PublishTask.status == "running",
            PublishTask.started_at.isnot(None),
            PublishTask.started_at < cutoff,
        )
        .values(
            status="pending",
            started_at=None,
            error_message="[Controller] Reclaimed stuck running publish task (timeout, crash mid-repodata, or hung createrepo). Will retry.",
        )
    )
    result = await db.execute(stmt)
    await db.flush()
    n = int(result.rowcount or 0)
    if n > 0:
        logger.warning(
            "Reclaimed %d publish_task row(s) stuck in running longer than %d minutes",
            n,
            minutes,
        )
    return n
