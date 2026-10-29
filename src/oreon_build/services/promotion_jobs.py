# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from oreon_build.core.audit import log_audit
from oreon_build.db.session import async_session_maker
from oreon_build.models import SideTag
from oreon_build.services.promotion_r2_cleanup import whole_tree_promote_branch
from oreon_build.services.side_tags import scrap_side_tag
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)

_jobs: dict[str, dict[str, Any]] = {}
_lock = asyncio.Lock()


def get_promotion_job(job_id: str) -> dict[str, Any] | None:
    row = _jobs.get(job_id)
    return dict(row) if row else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_job(job_id: str, **fields: Any) -> None:
    cur = _jobs.get(job_id) or {}
    cur.update(fields)
    cur["updated_at"] = _now()
    _jobs[job_id] = cur


async def _active_key_busy(key: str) -> bool:
    for row in _jobs.values():
        if row.get("key") == key and row.get("status") in ("queued", "running"):
            return True
    return False


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task[None]) -> None:
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.exception("promotion background job failed", exc_info=exc)

    task.add_done_callback(_done)


async def enqueue_branch_promote(
    *,
    release_id: int,
    releasename: str,
    from_branch: str,
    to_branch: str,
    arches: list[str],
    account_id: int | None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    key = f"branch:{release_id}:{from_branch}->{to_branch}"
    async with _lock:
        if await _active_key_busy(key):
            raise ValueError(f"Promote already running for {from_branch} -> {to_branch} on this release")
        job_id = uuid.uuid4().hex[:16]
        _jobs[job_id] = {
            "id": job_id,
            "kind": "branch",
            "key": key,
            "status": "queued",
            "release_id": release_id,
            "releasename": releasename,
            "from_branch": from_branch,
            "to_branch": to_branch,
            "created_at": _now(),
            "updated_at": _now(),
            "error": None,
            "r2": None,
        }
    _spawn(
        _run_branch_promote(
            job_id=job_id,
            release_id=release_id,
            releasename=releasename,
            from_branch=from_branch,
            to_branch=to_branch,
            arches=arches,
            account_id=account_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )
    )
    return get_promotion_job(job_id) or {"id": job_id, "status": "queued"}


async def enqueue_side_tag_promote(
    *,
    side_tag_id: int,
    releasename: str,
    from_branch: str,
    to_branch: str,
    arches: list[str],
    account_id: int | None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    key = f"side_tag:{side_tag_id}->{to_branch}"
    async with _lock:
        if await _active_key_busy(key):
            raise ValueError(f"Promote already running for side tag {from_branch} -> {to_branch}")
        job_id = uuid.uuid4().hex[:16]
        _jobs[job_id] = {
            "id": job_id,
            "kind": "side_tag",
            "key": key,
            "status": "queued",
            "side_tag_id": side_tag_id,
            "releasename": releasename,
            "from_branch": from_branch,
            "to_branch": to_branch,
            "created_at": _now(),
            "updated_at": _now(),
            "error": None,
            "r2": None,
            "side_tag_deleted": False,
        }
    _spawn(
        _run_side_tag_promote(
            job_id=job_id,
            side_tag_id=side_tag_id,
            releasename=releasename,
            from_branch=from_branch,
            to_branch=to_branch,
            arches=arches,
            account_id=account_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )
    )
    return get_promotion_job(job_id) or {"id": job_id, "status": "queued"}


async def _run_branch_promote(
    *,
    job_id: str,
    release_id: int,
    releasename: str,
    from_branch: str,
    to_branch: str,
    arches: list[str],
    account_id: int | None,
    client_ip: str | None,
    user_agent: str | None,
) -> None:
    _set_job(job_id, status="running")
    try:
        result = await asyncio.to_thread(
            whole_tree_promote_branch,
            releasename=releasename,
            from_branch=from_branch,
            to_branch=to_branch,
            arches=arches,
            empty_source=True,
            delete_source_prefix=False,
        )
        async with async_session_maker() as db:
            await log_audit(
                db,
                "promotion.branch",
                account_id=account_id,
                resource_type="release",
                resource_id=str(release_id),
                details=f"{from_branch} -> {to_branch} copied={result.get('copied')}",
                ip_address=client_ip,
                user_agent=user_agent,
            )
            await db.commit()
        _set_job(
            job_id,
            status="ok",
            r2=result,
            message=f"Copied then emptied source branch '{from_branch}'",
        )
        logger.info(
            "branch promote ok job=%s %s %s -> %s copied=%s",
            job_id,
            releasename,
            from_branch,
            to_branch,
            result.get("copied"),
        )
    except Exception as e:
        logger.exception("branch promote failed job=%s", job_id)
        _set_job(job_id, status="failed", error=str(e)[:2000])


async def _run_side_tag_promote(
    *,
    job_id: str,
    side_tag_id: int,
    releasename: str,
    from_branch: str,
    to_branch: str,
    arches: list[str],
    account_id: int | None,
    client_ip: str | None,
    user_agent: str | None,
) -> None:
    _set_job(job_id, status="running")
    try:
        result = await asyncio.to_thread(
            whole_tree_promote_branch,
            releasename=releasename,
            from_branch=from_branch,
            to_branch=to_branch,
            arches=arches,
            empty_source=True,
            delete_source_prefix=True,
        )
        scrap_info: dict[str, Any] | None = None
        async with async_session_maker() as db:
            tag = (
                await db.execute(
                    select(SideTag)
                    .where(SideTag.id == side_tag_id)
                    .options(selectinload(SideTag.release))
                )
            ).scalar_one_or_none()
            if tag is not None:
                scrap_info = await scrap_side_tag(db, tag, releasename=releasename)
            await log_audit(
                db,
                "side_tag.promote",
                account_id=account_id,
                resource_type="side_tag",
                resource_id=str(side_tag_id),
                details=f"{from_branch} -> {to_branch}",
                ip_address=client_ip,
                user_agent=user_agent,
            )
            await db.commit()
        _set_job(
            job_id,
            status="ok",
            r2=result,
            scrap=scrap_info,
            side_tag_deleted=True,
            message=f"Promoted side tag to {to_branch} and deleted the tag",
        )
        logger.info(
            "side tag promote ok job=%s %s %s -> %s",
            job_id,
            releasename,
            from_branch,
            to_branch,
        )
    except Exception as e:
        logger.exception("side tag promote failed job=%s", job_id)
        _set_job(job_id, status="failed", error=str(e)[:2000])
