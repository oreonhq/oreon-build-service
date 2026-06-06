"""Fire-and-forget build trigger work so HTTP handlers return before enqueue finishes."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select

from oreon_build.api.schemas import BuildTrigger, DistgitBatchTrigger, NewPackageForBuild, SourceTypeSchema
from oreon_build.db.session import async_session_maker
from oreon_build.models import Account, Release
from oreon_build.services.build_trigger_exec import execute_build_trigger

logger = logging.getLogger(__name__)


def _log_task_result(task: asyncio.Task[None]) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.exception("background build trigger failed", exc_info=exc)


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    task.add_done_callback(_log_task_result)


async def run_chain_trigger_enqueue(chain_id: int) -> None:
    from oreon_build.services.build_chain import enqueue_chain_jobs_after_create

    async with async_session_maker() as db:
        try:
            await enqueue_chain_jobs_after_create(db, chain_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def run_chain_resume_enqueue(chain_id: int, from_chain_id: int) -> None:
    from oreon_build.services.build_chain import enqueue_resumed_chain_stages

    async with async_session_maker() as db:
        try:
            await enqueue_resumed_chain_stages(db, chain_id, from_chain_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def run_distgit_batch(payload: dict[str, Any], user_id: int, ip: str | None, ua: str) -> None:
    data = DistgitBatchTrigger.model_validate(payload)
    async with async_session_maker() as db:
        user = await db.get(Account, user_id)
        if user is None:
            logger.error("distgit batch: user_id=%s not found", user_id)
            return
        rel_r = await db.execute(select(Release).where(Release.id == data.release_id))
        release = rel_r.scalar_one_or_none()
        if not release:
            logger.error("distgit batch: release_id=%s not found", data.release_id)
            return
        releasename = (release.releasename or "").strip()
        dg_url = (data.distgit_url or "https://github.com/oreonhq/rpm-specfiles.git").strip()
        dg_branch = (data.distgit_branch or "main").strip()

        queued = 0
        skipped = 0
        failed: list[str] = []
        for item in data.packages:
            pkg_name = (item.name or "").strip()
            if not pkg_name:
                continue
            spec_path = (item.spec_path or "").strip() or f"{releasename}/{pkg_name}/{pkg_name}.spec"
            combined_url = f"{dg_url.rstrip('/')}#{dg_branch}:{spec_path}"
            np = NewPackageForBuild(
                name=pkg_name,
                description=None,
                preferred_mock_environment_id=data.preferred_mock_environment_id,
                source_type=SourceTypeSchema.distgit,
                source_url=combined_url,
                source_branch=dg_branch,
                source_path=spec_path,
            )
            trigger = BuildTrigger(
                new_package=np,
                release_id=data.release_id,
                architectures=data.architectures,
                priority=data.priority,
                skip_complete_arches=data.skip_complete_arches,
            )
            try:
                jobs = await execute_build_trigger(
                    db,
                    user,
                    trigger,
                    request=None,
                    ip_address=ip,
                    user_agent=ua,
                )
                await db.commit()
                if jobs:
                    queued += 1
                else:
                    skipped += 1
            except Exception as e:
                await db.rollback()
                failed.append(f"{pkg_name}: {e}")
                logger.exception(
                    "distgit batch package=%s release_id=%s failed",
                    pkg_name,
                    data.release_id,
                )
        if queued or skipped or failed:
            logger.info(
                "distgit batch release_id=%s queued=%s skipped=%s failed=%s",
                data.release_id,
                queued,
                skipped,
                len(failed),
            )


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
