"""Background warmup for distgit provides index + solv caches."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_warmup_task: asyncio.Task[None] | None = None


async def _warmup_once() -> None:
    from oreon_build.config import get_settings
    from oreon_build.services.distgit_order import (
        _github_owner_repo_from_git_url,
        get_distgit_provides_index_cached,
        prune_distgit_memory_caches,
    )
    from oreon_build.services.repo_solver import get_repo_solver

    prune_distgit_memory_caches()

    settings = get_settings()
    tok = (getattr(settings, "github_token", None) or "").strip()
    headers = {"Accept": "application/vnd.github+json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    distgit_url = (getattr(settings, "mass_rebuild_distgit_url", None) or "").strip()
    distgit_branch = (getattr(settings, "mass_rebuild_distgit_branch", None) or "main").strip()
    gr = _github_owner_repo_from_git_url(distgit_url)
    if not gr:
        logger.info("distgit warmup skipped: no github distgit url")
        return
    owner, repo = gr

    # common release/branch combos for Oreon
    releases = ["oreon-11-rp1"]
    branches = sorted({distgit_branch, "testing/post-beta", "main"})
    for releasename in releases:
        for branch in branches:
            try:
                idx = await get_distgit_provides_index_cached(
                    owner=owner,
                    repo=repo,
                    distgit_branch=branch,
                    releasename=releasename,
                    headers=headers,
                )
                if idx is not None:
                    logger.info(
                        "distgit warmup ready %s/%s packages=%s",
                        releasename,
                        branch,
                        len(idx.package_names),
                    )
            except Exception:
                logger.warning(
                    "distgit warmup failed %s/%s",
                    releasename,
                    branch,
                    exc_info=True,
                )
        try:
            solver = get_repo_solver(
                releasename,
                "testing",
                ["x86_64"],
                extra_branches=["dev", "stable"],
            )
            await asyncio.to_thread(solver.warm)
            logger.info("solv warmup ready %s/testing", releasename)
        except Exception:
            logger.warning("solv warmup failed for %s", releasename, exc_info=True)
        # skip capability-index warmup. that object is multi-GB and OOMs this host
        # when layered on solv + side-tag reloads. load on demand only.


async def _warmup_loop() -> None:
    # first pass ASAP, then refresh tree SHA periodically
    while True:
        try:
            await _warmup_once()
        except Exception:
            logger.exception("distgit warmup loop error")
        await asyncio.sleep(600)


def spawn_distgit_warmup() -> None:
    global _warmup_task
    if _warmup_task is not None and not _warmup_task.done():
        return
    _warmup_task = asyncio.create_task(_warmup_loop())
    logger.info("distgit warmup task started")
