from __future__ import annotations

import asyncio
import logging
from typing import Any

from oreon_build.api.schemas import BuildTrigger, DistgitBatchTrigger, NewPackageForBuild, SourceTypeSchema
from oreon_build.core.repo_branch import release_distgit_branch
from oreon_build.db.session import async_session_maker
from oreon_build.models import Account, Release
from oreon_build.services.build_trigger_exec import execute_build_trigger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)

# one heavy seed at a time - dual kernel/plasma seeding OOMs the API
_plan_lock = asyncio.Lock()


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


async def run_distgit_batch(payload: dict[str, Any], user_id: int, ip: str | None, ua: str) -> None:
    data = DistgitBatchTrigger.model_validate(payload)
    async with async_session_maker() as db:
        user_result = await db.execute(
            select(Account).options(selectinload(Account.role)).where(Account.id == user_id)
        )
        user = user_result.scalar_one_or_none()
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
        dg_branch = (data.distgit_branch or release_distgit_branch(release)).strip()

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
                side_tag_id=int(data.side_tag_id),
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
                user_result = await db.execute(
                    select(Account).options(selectinload(Account.role)).where(Account.id == user_id)
                )
                user = user_result.scalar_one_or_none()
                if user is None:
                    logger.error("distgit batch: user_id=%s disappeared after rollback", user_id)
                    break
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


async def run_layer_seed(plan_id: int) -> None:
    from oreon_build.services.layer_engine import complete_seed_layer_plan

    async with _plan_lock:
        async with async_session_maker() as db:
            try:
                await complete_seed_layer_plan(db, int(plan_id))
                await db.commit()
                from oreon_build.services.layer_engine import evaluate_layer_buildability
                async with async_session_maker() as eval_db:
                    changed = await evaluate_layer_buildability(eval_db, int(plan_id))
                    await eval_db.commit()
                    logger.info(
                        "background layer seed evaluation complete layer_plan_id=%s queued=%s",
                        plan_id,
                        len(changed),
                    )
            except Exception as exc:
                await db.rollback()
                logger.exception("background layer seed layer_plan_id=%s failed", plan_id)
                try:
                    from oreon_build.models import LayerPlan, LayerPlanStatus
                    from oreon_build.services.layer_plan import fail_layer_plan

                    msg = f"Layer seeding failed: {exc}"
                    async with async_session_maker() as db2:
                        plan = await db2.get(LayerPlan, int(plan_id))
                        if plan is not None and plan.status == LayerPlanStatus.PENDING:
                            await fail_layer_plan(
                                db2,
                                plan,
                                msg[:8000],
                                terminal=LayerPlanStatus.FAILED,
                            )
                            await db2.commit()
                except Exception:
                    logger.exception(
                        "background layer seed cleanup failed layer_plan_id=%s", plan_id
                    )


async def run_layer_resume_refresh(plan_id: int, job_ids: list[int]) -> None:
    from oreon_build.services.layer_engine import evaluate_layer_buildability
    from oreon_build.services.layer_plan import maybe_settle_layer_plan

    try:
        async with async_session_maker() as db:
            changed = await evaluate_layer_buildability(db, int(plan_id))
            await maybe_settle_layer_plan(db, int(plan_id))
            await db.commit()
            logger.info(
                "background layer resume evaluation complete layer_plan_id=%s queued=%s",
                plan_id,
                len(changed),
            )
    except Exception:
        logger.exception("background layer resume evaluation failed layer_plan_id=%s", plan_id)


def schedule_layer_resume_refresh(plan_id: int, job_ids: list[int]) -> None:
    spawn(run_layer_resume_refresh(int(plan_id), list(job_ids or [])))


async def run_layer_terminal_notify(job_id: int, status_str: str, error_message: str | None) -> None:
    """Layer autofix/settle for a terminal job. Serializes on the plan row, so it
    must never run inside the worker result request."""
    from oreon_build.services.layer_plan import notify_layer_job_terminal_after_result

    try:
        await notify_layer_job_terminal_after_result(int(job_id), status_str, error_message)
    except Exception:
        logger.exception("background layer terminal notify failed job_id=%s", job_id)


def schedule_layer_terminal_notify(
    job_id: int, status_str: str, error_message: str | None
) -> None:
    spawn(run_layer_terminal_notify(int(job_id), status_str, error_message))
