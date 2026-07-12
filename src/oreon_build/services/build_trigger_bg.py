from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select

from oreon_build.api.schemas import BuildTrigger, DistgitBatchTrigger, NewPackageForBuild, SourceTypeSchema
from oreon_build.core.repo_branch import release_distgit_branch
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


async def _fail_chain_planning(
    chain_id: int,
    message: str,
    *,
    plan_errors: list[str] | None = None,
) -> None:
    from oreon_build.core.audit import log_audit
    from oreon_build.models import BuildChain
    from oreon_build.services.build_chain import (
        _options_dict,
        _set_chain_options,
        fail_chain,
    )
    from oreon_build.services.discord import DISCORD_COLOR_FAILED, send_discord_embed

    msg = (message or "Dependency planning failed").strip()
    async with async_session_maker() as db:
        chain_r = await db.execute(select(BuildChain).where(BuildChain.id == chain_id).with_for_update())
        chain = chain_r.scalar_one_or_none()
        if not chain:
            return
        await fail_chain(db, chain, msg)
        opt = _options_dict(chain)
        opt.pop("plan_pending", None)
        opt.pop("plan_roots", None)
        if plan_errors:
            opt["plan_warnings"] = plan_errors[:40]
        _set_chain_options(chain, opt)
        await log_audit(
            db,
            "build.chain_plan_failed",
            account_id=chain.triggered_by_id,
            resource_type="build_chain",
            resource_id=str(chain.id),
            details=msg[:1500],
            emit_discord=False,
        )
        await db.commit()

    try:
        await send_discord_embed(
            title=f"Chain {chain_id} planning failed",
            description=msg[:1800],
            fields=[("Chain", str(chain_id)), ("Status", "failed")],
            color=DISCORD_COLOR_FAILED,
        )
    except Exception:
        logger.exception("discord notify failed for chain plan failure chain_id=%s", chain_id)


async def run_chain_trigger_enqueue(chain_id: int) -> None:
    from oreon_build.services.build_chain import enqueue_chain_jobs_after_create

    async with async_session_maker() as db:
        try:
            await enqueue_chain_jobs_after_create(db, chain_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def run_auto_chain_plan_and_enqueue(chain_id: int) -> None:
    import os

    from sqlalchemy import select

    from oreon_build.config import get_settings
    from oreon_build.core.repo_branch import r2_publish_branch, release_distgit_branch
    from oreon_build.models import BuildChain, Release
    from oreon_build.services.build_chain import (
        apply_automatic_plan_to_chain,
        enqueue_chain_jobs_after_create,
        fail_chain,
        _options_dict,
        _set_chain_options,
    )
    from oreon_build.services.build_planner import fetch_and_plan_automatic_chain

    plan_params: dict[str, Any] | None = None
    async with async_session_maker() as db:
        chain_r = await db.execute(select(BuildChain).where(BuildChain.id == chain_id).with_for_update())
        chain = chain_r.scalar_one_or_none()
        if not chain:
            logger.error("auto chain plan: chain_id=%s not found", chain_id)
            return
        opt = _options_dict(chain)
        if not opt.get("plan_pending"):
            try:
                await enqueue_chain_jobs_after_create(db, chain_id)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            return
        roots = [str(r) for r in (opt.get("plan_roots") or []) if r]
        if not roots:
            await db.commit()
            await _fail_chain_planning(chain_id, "Auto plan missing root packages")
            return
        rr = await db.execute(select(Release).where(Release.id == chain.release_id))
        release = rr.scalar_one_or_none()
        if not release:
            await db.commit()
            await _fail_chain_planning(chain_id, "Release not found for chain planning")
            return
        from oreon_build.models import MockEnvironment
        from oreon_build.services.repo_capability_index import (
            default_capability_extra_branches,
            repo_channels_from_mock_urls,
        )

        releasename = (release.releasename or "").strip()
        publish_branch = r2_publish_branch(release)
        mock_urls: list[str] = []
        mock_envs = list(
            (
                await db.execute(
                    select(MockEnvironment).where(
                        MockEnvironment.release_id == release.id,
                        MockEnvironment.is_available.is_(True),
                    )
                )
            ).scalars().all()
        )
        pref_mock = opt.get("preferred_mock_environment_id")
        if pref_mock:
            preferred = next((e for e in mock_envs if e.id == int(pref_mock)), None)
            if preferred is not None:
                mock_envs = [preferred] + [e for e in mock_envs if e.id != preferred.id]
        for env in mock_envs:
            mock_urls.extend(str(u).strip() for u in (env.additional_repo_urls or []) if str(u).strip())
            mock_urls.extend(str(u).strip() for u in (env.lookaside_repo_urls or []) if str(u).strip())
        extra_branches = default_capability_extra_branches(publish_branch)
        for ch in repo_channels_from_mock_urls(releasename, mock_urls):
            if ch != publish_branch and ch not in extra_branches:
                extra_branches.append(ch)
        plan_params = {
            "releasename": releasename,
            "repo_branch": publish_branch,
            "distgit_url": str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip(),
            "distgit_branch": str(opt.get("distgit_branch") or release_distgit_branch(release)).strip(),
            "root_packages": roots,
            "architectures": [str(x) for x in opt.get("architectures") or ["x86_64", "aarch64"]],
            "capability_extra_branches": extra_branches,
        }
        logger.info(
            "chain_id=%s plan roots=%s publish=%s capability_extra=%s",
            chain_id,
            roots,
            publish_branch,
            extra_branches,
        )
        await db.commit()

    settings = get_settings()
    token = (getattr(settings, "github_token", None) or os.environ.get("GITHUB_TOKEN") or "").strip() or None
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        plan, t_errs = await fetch_and_plan_automatic_chain(headers=headers, **plan_params)
    except Exception:
        logger.exception("auto chain plan failed chain_id=%s", chain_id)
        await _fail_chain_planning(chain_id, "Dependency planning failed")
        return

    if t_errs:
        logger.warning("chain_id=%s distgit fetch errors: %s", chain_id, t_errs)
    if not plan.chain_spec.strip() and not plan.package_plans:
        msg = plan.errors[0] if plan.errors else "Could not plan chain dependencies"
        await _fail_chain_planning(chain_id, msg, plan_errors=plan.errors)
        return

    if plan.blocking_errors:
        pkgs = ", ".join(plan.blocking_errors[:40])
        if len(plan.blocking_errors) > 40:
            pkgs += ", ..."
        msg = (
            "Dependency planning blocked, please import the following into rpm-specfiles:\n"
            + pkgs
        )
        await _fail_chain_planning(chain_id, msg, plan_errors=plan.errors)
        return

    if plan.errors:
        logger.warning("chain_id=%s plan warnings: %s", chain_id, plan.errors)

    spec = plan.chain_spec.strip() or " ".join(plan_params["root_packages"])
    async with async_session_maker() as db:
        try:
            chain_r = await db.execute(select(BuildChain).where(BuildChain.id == chain_id).with_for_update())
            chain = chain_r.scalar_one_or_none()
            if not chain:
                logger.error("auto chain plan apply: chain_id=%s not found", chain_id)
                return
            apply_automatic_plan_to_chain(
                chain,
                chain_spec=spec,
                package_plans=plan.package_plans,
            )
            await enqueue_chain_jobs_after_create(db, chain_id)
            await db.commit()
            logger.info(
                "chain_id=%s planned build=%s skip=%s",
                chain_id,
                len(plan.package_plans),
                len(plan.skipped),
            )
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
