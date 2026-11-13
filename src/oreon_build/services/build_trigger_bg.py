# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging
from typing import Any

from oreon_build.api.schemas import BuildTrigger
from oreon_build.db.session import async_session_maker
from oreon_build.models import Account
from oreon_build.services.build_trigger_exec import execute_build_trigger

logger = logging.getLogger(__name__)

_running: set[asyncio.Task] = set()


def _log_task_result(task: asyncio.Task[None]) -> None:
    _running.discard(task)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.exception("background build trigger failed", exc_info=exc)


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _running.add(task)
    task.add_done_callback(_log_task_result)


async def run_single_trigger(payload: dict[str, Any], user_id: int, ip: str | None, ua: str) -> None:
    data = BuildTrigger.model_validate(payload)
    async with async_session_maker() as db:
        user = await db.get(Account, user_id)
        if user is None:
            logger.error("build trigger: user_id=%s not found", user_id)
            return
        try:
            await execute_build_trigger(
                db,
                user,
                data,
                request=None,
                ip_address=ip,
                user_agent=ua,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("background build trigger release_id=%s failed", data.release_id)
