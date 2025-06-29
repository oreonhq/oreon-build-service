"""Scheduler: cron-style jobs, dependency rebuilds, compose triggers."""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


async def run_scheduled_tasks() -> None:
    from oreon_build.db.session import async_session_maker
    from oreon_build.models import Schedule
    from sqlalchemy import select
    async with async_session_maker() as db:
        result = await db.execute(
            select(Schedule).where(Schedule.is_enabled == True)
        )
        for s in result.scalars().all():
            logger.info("Schedule run: %s kind=%s", s.name, s.kind)
            # TODO: trigger build or compose based on s.kind and s.config
    return None


async def main_async() -> None:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_scheduled_tasks,
        CronTrigger.from_crontab("*/5 * * * *"),
        id="oreon_schedules",
    )
    scheduler.start()
    logger.info("Oreon scheduler started")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    scheduler.shutdown()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
