"""Distgit chain builds: colon-separated parallel groups, sequential stages, gated on repo publish."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select

from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.chain_build_spec import parse_chain_build_spec
from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import (
    BuildAttempt,
    BuildChain,
    BuildChainPublishReceipt,
    BuildChainStatus,
    BuildJob,
    BuildStatus,
    MockEnvironment,
    Package,
    PackageVersion,
    PublishTask,
    Release,
    Source,
    Worker,
)
from oreon_build.services.build_target_util import (
    get_or_create_build_target,
    has_successful_build_for_target,
    latest_package_version_id,
)

if TYPE_CHECKING:
    from oreon_build.models import Account

logger = logging.getLogger(__name__)

DEFAULT_ARCH = ["x86_64", "aarch64"]

# Cache distgit order for chain dep gating (GitHub + spec parse is expensive).
_chain_distgit_order_cache: dict[tuple[str, str, str], tuple[float, Any]] = {}


def _chain_use_distgit_dep_gate() -> bool:
    return (os.getenv("OREON_CHAIN_USE_DISTGIT_ORDER_GATE") or "1").strip().lower() not in ("0", "false", "no")


def _chain_dep_poll_sec() -> int:
    raw = (os.getenv("OREON_CHAIN_DEPS_POLL_SEC") or "20").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 20
    return max(5, min(n, 600))


def _chain_distgit_order_cache_ttl_sec() -> float:
    raw = (os.getenv("OREON_CHAIN_DISTGIT_ORDER_CACHE_SEC") or "300").strip()
    try:
        n = float(raw)
    except ValueError:
        n = 300.0
    return max(30.0, min(n, 3600.0))


def _predecessor_names_in_tree(order_edges: list[dict[str, str]], consumer: str) -> set[str]:
    return {str(e["to"]) for e in order_edges if str(e.get("from")) == consumer}


def _deps_map_from_edges(order_edges: list[dict[str, str]]) -> dict[str, set[str]]:
    deps: dict[str, set[str]] = {}
    for e in order_edges:
        cons = str(e.get("from") or "")
        prov = str(e.get("to") or "")
        if cons and prov:
            deps.setdefault(cons, set()).add(prov)
    return deps


def _transitive_spec_predecessors(
    deps_map: dict[str, set[str]],
    roots: list[str],
    spec_names: set[str],
) -> set[str]:
    """All distgit spec providers needed by roots, walking BuildRequires transitively."""
    needed: set[str] = set()
    stack = [str(r) for r in roots if r]
    seen: set[str] = set()
    while stack:
        pkg = stack.pop()
        for pred in deps_map.get(pkg, set()):
            if pred not in spec_names:
                continue
            if pred in seen:
                continue
            seen.add(pred)
            needed.add(pred)
            stack.append(pred)
    return needed


def _layers_for_package_subset(
    needed: set[str],
    order_layers: list[list[str]],
) -> list[list[str]]:
    """Topological layers for ``needed``, preserving distgit layer order."""
    if not needed:
        return []
    out: list[list[str]] = []
    placed: set[str] = set()
    for layer in order_layers:
        sub = sorted(p for p in layer if p in needed)
        if sub:
            out.append(sub)
            placed.update(sub)
    leftover = sorted(needed - placed)
    if leftover:
        out.insert(0, leftover)
    return out


def _parse_skip_jobs_not_before(chain: BuildChain) -> datetime | None:
    opt = _options_dict(chain)
    raw = opt.get("skip_jobs_not_before")
    if not raw:
        ca = getattr(chain, "created_at", None)
        if ca is not None:
            return ca if ca.tzinfo else ca.replace(tzinfo=timezone.utc)
        return None
    try:
        ref = datetime.fromisoformat(str(raw))
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        return ref
    except ValueError:
        return None


async def _package_id_for_name(db: Any, name: str) -> int | None:
    prow = await db.execute(select(Package.id).where(Package.name == name).limit(1))
    pid = prow.scalar_one_or_none()
    return int(pid) if pid is not None else None


async def _chain_has_successful_build_all_arches(
    db: Any,
    chain: BuildChain,
    package_id: int,
    *,
    architectures: list[str],
    preferred_mock_environment_id: int | None,
    skip_jobs_not_before: datetime | None,
) -> bool:
    """True when every chain architecture already has a successful build for this package."""
    if not architectures:
        return False
    pv_id = await latest_package_version_id(db, package_id)
    for arch in architectures:
        canon = normalize_machine_arch(arch)
        target = await get_or_create_build_target(db, chain.release_id, canon, preferred_mock_environment_id)
        if not await has_successful_build_for_target(
            db,
            package_id,
            chain.release_id,
            target.id,
            pv_id,
            jobs_created_on_or_after=skip_jobs_not_before,
        ):
            return False
    return True


async def _cached_distgit_build_order(releasename: str, distgit_url: str, distgit_branch: str) -> Any:
    from oreon_build.services.distgit_order import compute_distgit_build_order

    k = (releasename.strip(), distgit_url.strip(), distgit_branch.strip())
    now = time.monotonic()
    ttl = _chain_distgit_order_cache_ttl_sec()
    hit = _chain_distgit_order_cache.get(k)
    if hit is not None and (now - hit[0]) < ttl:
        return hit[1]
    order = await compute_distgit_build_order(releasename=k[0], distgit_url=k[1], distgit_branch=k[2])
    _chain_distgit_order_cache[k] = (now, order)
    return order


async def _inject_missing_dep_layers_before(
    db: Any,
    chain: BuildChain,
    stage_index: int,
) -> bool:
    """
    Insert distgit dependency stages immediately before ``stage_index``.

  * Walks BuildRequires transitively (not only direct deps).
  * Only injects packages that exist in the distgit spec tree for this release.
  * Skips deps already listed anywhere in the chain spec stages.
  * Skips deps that already have a successful build on every chain architecture.
  * If a dep was in an earlier chain stage but still has no success, hard-fail
    (user must fix that job or resume the chain).

    Returns False when ``chain`` is marked failed.
    """
    if not _chain_use_distgit_dep_gate():
        return True

    stages: list[list[str]] = chain.stages_json if isinstance(chain.stages_json, list) else []
    if stage_index < 0 or stage_index >= len(stages):
        return True

    pkgs = [str(p) for p in stages[stage_index] if p]
    if not pkgs:
        return True

    rr = await db.execute(select(Release).where(Release.id == chain.release_id))
    release = rr.scalar_one_or_none()
    if not release:
        return True

    opt = _options_dict(chain)
    dg_url = str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git")
    dg_br = str(opt.get("distgit_branch") or "main")
    releasename = (release.releasename or "").strip()
    if not releasename:
        return True

    arches = [str(x) for x in opt.get("architectures") or DEFAULT_ARCH]
    pm = opt.get("preferred_mock_environment_id")
    if pm is None or pm == "":
        pref_pm = None
    else:
        try:
            pref_pm = int(pm)
        except (TypeError, ValueError):
            pref_pm = None
    skip_not_before = _parse_skip_jobs_not_before(chain) if bool(opt.get("skip_complete_arches", True)) else None

    order = await _cached_distgit_build_order(releasename, dg_url, dg_br)
    edges = order.edges if isinstance(order.edges, list) else []
    order_layers = order.layers if isinstance(order.layers, list) else []
    spec_names = {str(n) for n in (order.package_names or []) if n}

    deps_map = _deps_map_from_edges(edges)
    all_chain_pkg_names: set[str] = {str(p).lower() for stage in stages for p in stage if p}
    past_stage_pkg_names: set[str] = {
        str(p).lower() for stage in stages[:stage_index] for p in stage if p
    }

    candidates = _transitive_spec_predecessors(deps_map, pkgs, spec_names)
    to_inject: set[str] = set()

    for pred in candidates:
        pred_lower = pred.lower()
        if pred_lower in all_chain_pkg_names:
            continue

        pid = await _package_id_for_name(db, pred)
        if pid is None:
            # No DB row yet; still inject if it is in distgit (enqueue will create Package).
            if pred not in spec_names:
                continue
        elif await _chain_has_successful_build_all_arches(
            db,
            chain,
            pid,
            architectures=arches,
            preferred_mock_environment_id=pref_pm,
            skip_jobs_not_before=skip_not_before,
        ):
            continue

        if pred_lower in past_stage_pkg_names:
            if pid is not None and await _chain_has_successful_build_all_arches(
                db,
                chain,
                pid,
                architectures=arches,
                preferred_mock_environment_id=pref_pm,
                skip_jobs_not_before=skip_not_before,
            ):
                continue
            msg = (
                f"Chain blocked: dependency '{pred}' was in an earlier stage but has no "
                f"successful build for this release on all chain architectures. "
                f"Fix or rebuild '{pred}', then resume the chain."
            )
            chain.status = BuildChainStatus.FAILED
            chain.error_message = msg
            logger.error("chain_id=%s hard-fail: %s", chain.id, msg)
            return False

        to_inject.add(pred)

    layers_to_insert = _layers_for_package_subset(to_inject, order_layers)
    if not layers_to_insert:
        return True

    new_stages = list(stages)
    for offset, layer in enumerate(layers_to_insert):
        new_stages.insert(stage_index + offset, layer)
    chain.stages_json = new_stages
    await db.flush()

    logger.info(
        "chain_id=%s auto-injected %s dep stage(s) before index %s for %s",
        chain.id,
        len(layers_to_insert),
        stage_index,
        pkgs,
    )
    return True


async def _cancel_chain_stage_active_jobs(
    db: Any,
    chain: BuildChain,
    stage_index: int,
    note: str,
) -> None:
    jres = await db.execute(
        select(BuildJob).where(
            BuildJob.chain_id == chain.id,
            BuildJob.chain_stage_index == stage_index,
            BuildJob.status.in_(
                (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING),
            ),
        )
    )
    for job in jres.scalars().all():
        await _cancel_job_and_running_attempts(db, job, note)


async def _try_recover_chain_after_failure(
    db: Any,
    chain: BuildChain,
    failed_job: BuildJob,
) -> bool:
    """
    On a stage failure, inject missing distgit deps before the active stage and
    re-queue that stage instead of failing the whole chain.
    """
    if failed_job.chain_stage_index is None:
        return False
    stage_idx = int(failed_job.chain_stage_index)
    stages_before = chain.stages_json if isinstance(chain.stages_json, list) else []
    stage_count_before = len(stages_before)

    ok = await _inject_missing_dep_layers_before(db, chain, stage_idx)
    if not ok:
        return False

    stages_after = chain.stages_json if isinstance(chain.stages_json, list) else []
    if len(stages_after) <= stage_count_before:
        return False

    await _cancel_chain_stage_active_jobs(
        db,
        chain,
        stage_idx,
        "Chain paused: building missing dependencies first",
    )
    chain.stage_expected_jobs = 0
    chain.stage_skip_slots = 0
    chain.stage_publish_done = 0
    chain.stage_advance_after = None

    opt = _options_dict(chain)
    arches = [str(x) for x in opt.get("architectures") or DEFAULT_ARCH]
    pm = opt.get("preferred_mock_environment_id")
    if pm is None or pm == "":
        pref_pm = None
    else:
        try:
            pref_pm = int(pm)
        except (TypeError, ValueError):
            pref_pm = None

    await _enqueue_chain_stage_jobs(
        db,
        chain,
        stage_idx,
        architectures=arches,
        skip_complete_arches=bool(opt.get("skip_complete_arches", True)),
        distgit_url=str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git"),
        distgit_branch=str(opt.get("distgit_branch") or "main"),
        preferred_mock_environment_id=pref_pm,
        priority=int(opt.get("priority") or 0),
        triggered_by_id=chain.triggered_by_id,
        request=None,
    )
    logger.info(
        "chain_id=%s recovered from job %s failure by injecting deps before stage %s",
        chain.id,
        failed_job.id,
        stage_idx,
    )
    return True


def _chain_stage_settle_sec() -> int:
    """Seconds to wait after all stage publishes complete before advancing.

    Gives workers time to see fresh repodata before the next stage's jobs
    start. Configurable via OREON_CHAIN_STAGE_SETTLE_SEC (default 30).
    Set to 0 to advance immediately (old behavior).
    """
    raw = (os.environ.get("OREON_CHAIN_STAGE_SETTLE_SEC") or "30").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 30
    return max(0, min(n, 600))


def distgit_new_package_dict(
    pkg: str,
    releasename: str,
    *,
    distgit_url: str,
    distgit_branch: str,
    preferred_mock_environment_id: int | None,
) -> dict[str, Any]:
    spec_rel = f"{releasename}/{pkg}/{pkg}.spec"
    url = f"{distgit_url.rstrip('/')}#{distgit_branch}:{spec_rel}"
    return {
        "name": pkg,
        "description": None,
        "preferred_mock_environment_id": preferred_mock_environment_id,
        "github_repository_id": None,
        "github_html_url": "https://github.com/oreonhq/rpm-specfiles",
        "source_type": "distgit",
        "source_url": url,
        "source_urls": None,
        "source_branch": distgit_branch,
        "source_path": spec_rel,
        "version": "0.0.1",
    }


def _infer_github_html_url_from_distgit(url: str) -> str | None:
    if not url or "github.com" not in url.lower():
        return None
    base = url.split("#", 1)[0].strip().rstrip("/")
    if base.lower().endswith(".git"):
        base = base[:-4]
    if not base.lower().startswith("http"):
        return None
    return base


async def _cancel_job_and_running_attempts(db: Any, job: BuildJob, note: str) -> None:
    if job.status not in (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING):
        return
    job.status = BuildStatus.CANCELLED
    job.completed_at = datetime.now(timezone.utc)
    att_result = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.build_job_id == job.id,
            BuildAttempt.status == BuildStatus.RUNNING,
        )
    )
    now = datetime.now(timezone.utc)
    affected_worker_ids: set[int] = set()
    for att in att_result.scalars().all():
        att.status = BuildStatus.CANCELLED
        att.finished_at = now
        att.error_message = note
        if att.signing_status in ("not_started", "running", "uploading"):
            att.signing_status = "failed"
        if att.worker_id is not None:
            affected_worker_ids.add(att.worker_id)
    if affected_worker_ids:
        await db.flush()
        from oreon_build.models import Worker as WorkerModel
        from oreon_build.services.worker_state_sync import sync_worker_state_after_attempt_finished

        for wid in affected_worker_ids:
            w = await db.get(WorkerModel, wid)
            if w is not None:
                await sync_worker_state_after_attempt_finished(db, w)


async def fail_chain(
    db: Any,
    chain: BuildChain,
    message: str,
    *,
    terminal: BuildChainStatus = BuildChainStatus.FAILED,
) -> None:
    """Mark the chain terminal and cancel every still-active job in the chain (any stage)."""
    if chain.status in (
        BuildChainStatus.SUCCESS,
        BuildChainStatus.FAILED,
        BuildChainStatus.CANCELLED,
    ):
        return
    chain.status = terminal
    chain.error_message = (message or "")[:4000]
    jres = await db.execute(
        select(BuildJob).where(
            BuildJob.chain_id == chain.id,
            BuildJob.status.in_(
                (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING),
            ),
        )
    )
    for j in jres.scalars().all():
        await _cancel_job_and_running_attempts(db, j, "Chain build stopped: " + (message or terminal.value))


async def on_build_job_failed_for_chain(db: Any, job: BuildJob, reason: str) -> None:
    if job.chain_id is None:
        return
    cres = await db.execute(select(BuildChain).where(BuildChain.id == job.chain_id).with_for_update())
    chain = cres.scalar_one_or_none()
    if not chain or chain.status not in (BuildChainStatus.PENDING, BuildChainStatus.RUNNING):
        return
    if job.chain_stage_index is not None and job.chain_stage_index != chain.active_stage_index:
        return
    if await _try_recover_chain_after_failure(db, chain, job):
        return
    await fail_chain(db, chain, reason, terminal=BuildChainStatus.FAILED)


async def on_user_cancelled_chain_job(db: Any, job: BuildJob) -> None:
    if job.chain_id is None:
        return
    cres = await db.execute(select(BuildChain).where(BuildChain.id == job.chain_id).with_for_update())
    chain = cres.scalar_one_or_none()
    if not chain or chain.status not in (BuildChainStatus.PENDING, BuildChainStatus.RUNNING):
        return
    await fail_chain(db, chain, "Cancelled by user", terminal=BuildChainStatus.CANCELLED)


async def on_repo_publish_success_for_chain(attempt_id: int) -> None:
    from oreon_build.db.session import async_session_maker

    async with async_session_maker() as db:
        att_r = await db.execute(select(BuildAttempt).where(BuildAttempt.id == attempt_id))
        att = att_r.scalar_one_or_none()
        if not att:
            return
        job_r = await db.execute(select(BuildJob).where(BuildJob.id == att.build_job_id))
        job = job_r.scalar_one_or_none()
        if not job or job.chain_id is None:
            return

        chain_r = await db.execute(select(BuildChain).where(BuildChain.id == job.chain_id).with_for_update())
        chain = chain_r.scalar_one_or_none()
        if not chain or chain.status not in (BuildChainStatus.PENDING, BuildChainStatus.RUNNING):
            return
        if job.chain_stage_index is None or job.chain_stage_index != chain.active_stage_index:
            return

        if chain.status == BuildChainStatus.PENDING:
            chain.status = BuildChainStatus.RUNNING

        rec = BuildChainPublishReceipt(build_chain_id=chain.id, build_attempt_id=attempt_id)
        db.add(rec)
        try:
            await db.flush()
        except Exception:
            await db.rollback()
            return

        chain.stage_publish_done += 1

        if chain.stage_publish_done >= chain.stage_expected_jobs:
            settle = _chain_stage_settle_sec()
            if settle > 0:
                # Persist the advance-after timestamp so the background loop picks it
                # up even if this process restarts during the settle window.
                chain.stage_advance_after = datetime.now(timezone.utc) + timedelta(seconds=settle)
                logger.info(
                    "chain_id=%s stage=%s all publishes done; advancing in %ss at %s",
                    chain.id,
                    chain.active_stage_index,
                    settle,
                    chain.stage_advance_after.isoformat(),
                )
            else:
                # Settle disabled: advance immediately (old behavior).
                await _advance_chain_after_stage_complete(db, chain)

        await db.commit()


async def on_repo_publish_failed_for_chain(attempt_id: int, err: str) -> None:
    from oreon_build.db.session import async_session_maker

    async with async_session_maker() as db:
        att_r = await db.execute(select(BuildAttempt).where(BuildAttempt.id == attempt_id))
        att = att_r.scalar_one_or_none()
        if not att:
            return
        job_r = await db.execute(select(BuildJob).where(BuildJob.id == att.build_job_id))
        job = job_r.scalar_one_or_none()
        if not job or job.chain_id is None:
            return
        chain_r = await db.execute(select(BuildChain).where(BuildChain.id == job.chain_id).with_for_update())
        chain = chain_r.scalar_one_or_none()
        if not chain or chain.status not in (BuildChainStatus.PENDING, BuildChainStatus.RUNNING):
            return
        if job.chain_stage_index is None or job.chain_stage_index != chain.active_stage_index:
            return
        await fail_chain(db, chain, f"Repo publish failed for chain: {err[:500]}", terminal=BuildChainStatus.FAILED)
        await db.commit()


def _options_dict(chain: BuildChain) -> dict[str, Any]:
    raw = chain.chain_options
    return raw if isinstance(raw, dict) else {}


async def _advance_chain_after_stage_complete(db: Any, chain: BuildChain) -> None:
    stages: list[list[str]] = chain.stages_json if isinstance(chain.stages_json, list) else []
    nxt = chain.active_stage_index + 1
    if nxt >= len(stages):
        chain.status = BuildChainStatus.SUCCESS
        chain.stage_expected_jobs = 0
        chain.stage_skip_slots = 0
        chain.stage_publish_done = 0
        chain.stage_advance_after = None
        return

    if _chain_use_distgit_dep_gate():
        ok = await _inject_missing_dep_layers_before(db, chain, nxt)
        if not ok:
            # Hard-fail: chain.status and chain.error_message already set.
            await db.flush()
            return
        # stages_json may have grown; re-read so nxt still points to the right stage.
        stages = chain.stages_json if isinstance(chain.stages_json, list) else []
        if nxt >= len(stages):
            chain.status = BuildChainStatus.SUCCESS
            chain.stage_expected_jobs = 0
            chain.stage_skip_slots = 0
            chain.stage_publish_done = 0
            chain.stage_advance_after = None
            return

    chain.active_stage_index = nxt
    chain.stage_expected_jobs = 0
    chain.stage_skip_slots = 0
    chain.stage_publish_done = 0
    chain.stage_advance_after = None
    await db.flush()

    opt = _options_dict(chain)
    arches = [str(x) for x in opt.get("architectures") or DEFAULT_ARCH]
    pm = opt.get("preferred_mock_environment_id")
    if pm is None or pm == "":
        pref_pm = None
    else:
        try:
            pref_pm = int(pm)
        except (TypeError, ValueError):
            pref_pm = None
    await _enqueue_chain_stage_jobs(
        db,
        chain,
        nxt,
        architectures=arches,
        skip_complete_arches=bool(opt.get("skip_complete_arches", True)),
        distgit_url=str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git"),
        distgit_branch=str(opt.get("distgit_branch") or "main"),
        preferred_mock_environment_id=pref_pm,
        priority=int(opt.get("priority") or 0),
        triggered_by_id=chain.triggered_by_id,
        request=None,
    )


async def _enqueue_chain_stage_jobs(
    db: Any,
    chain: BuildChain,
    stage_index: int,
    *,
    architectures: list[str],
    skip_complete_arches: bool,
    distgit_url: str,
    distgit_branch: str,
    preferred_mock_environment_id: int | None,
    priority: int,
    triggered_by_id: int | None,
    request: Request | None,
) -> list[BuildJob]:
    stages: list[list[str]] = chain.stages_json if isinstance(chain.stages_json, list) else []
    if stage_index < 0 or stage_index >= len(stages):
        return []
    pkgs = [str(p) for p in stages[stage_index] if p]
    if not pkgs:
        return []

    rr = await db.execute(select(Release).where(Release.id == chain.release_id))
    release = rr.scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")

    workers_result = await db.execute(select(Worker))
    workers = list(workers_result.scalars().all())
    workers_by_arch: dict[str, bool] = {}
    for w in workers:
        workers_by_arch[normalize_machine_arch(w.architecture)] = True

    jobs_created: list[BuildJob] = []
    expected_jobs = 0
    skip_slots = 0
    publish_branch = r2_publish_branch(release)
    releasename = (release.releasename or "").strip()

    skip_jobs_not_before: datetime | None = None
    opt_raw = chain.chain_options if isinstance(chain.chain_options, dict) else {}
    raw_skip = opt_raw.get("skip_jobs_not_before")
    if skip_complete_arches and raw_skip:
        try:
            skip_jobs_not_before = datetime.fromisoformat(str(raw_skip))
            if skip_jobs_not_before.tzinfo is None:
                skip_jobs_not_before = skip_jobs_not_before.replace(tzinfo=timezone.utc)
        except ValueError:
            skip_jobs_not_before = None
    if skip_complete_arches and skip_jobs_not_before is None:
        ca = getattr(chain, "created_at", None)
        if ca is not None:
            skip_jobs_not_before = ca if ca.tzinfo else ca.replace(tzinfo=timezone.utc)

    for pkg_name in pkgs:
        existing_r = await db.execute(select(Package).where(Package.name == pkg_name))
        existing_pkg = existing_r.scalar_one_or_none()
        package_id: int
        package_version_id: int | None
        pref_mock: int | None

        if existing_pkg:
            package_id = existing_pkg.id
            package_version_id = await latest_package_version_id(db, package_id)
            # Chain-specified mock takes priority; fall back to package's stored preference.
            pref_mock = preferred_mock_environment_id or existing_pkg.preferred_mock_environment_id
        else:
            npd = distgit_new_package_dict(
                pkg_name,
                releasename,
                distgit_url=distgit_url,
                distgit_branch=distgit_branch,
                preferred_mock_environment_id=preferred_mock_environment_id,
            )
            gh_url = _infer_github_html_url_from_distgit(npd["source_url"] or "")
            pkg = Package(
                name=npd["name"],
                description=npd["description"],
                preferred_mock_environment_id=npd["preferred_mock_environment_id"],
                github_repository_id=npd["github_repository_id"],
                github_html_url=gh_url,
            )
            db.add(pkg)
            await db.flush()
            package_id = pkg.id
            pv = PackageVersion(package_id=pkg.id, version=npd["version"] or "0.0.1")
            db.add(pv)
            await db.flush()
            package_version_id = pv.id
            if npd.get("source_url"):
                db.add(Source(package_version_id=pv.id, kind="distgit", url=npd["source_url"]))
            await db.flush()
            pref_mock = npd["preferred_mock_environment_id"]
            if triggered_by_id is not None:
                await log_audit(
                    db,
                    "package.create",
                    account_id=triggered_by_id,
                    resource_type="package",
                    resource_id=str(pkg.id),
                    details=pkg.name,
                    ip_address=get_client_ip(request) if request is not None else None,
                    user_agent=get_user_agent(request) if request is not None else "chain-build",
                )

        for arch in architectures:
            canon = normalize_machine_arch(arch)
            if not workers_by_arch.get(canon, False):
                continue
            target = await get_or_create_build_target(db, chain.release_id, canon, pref_mock)
            if skip_complete_arches and await has_successful_build_for_target(
                db,
                package_id,
                chain.release_id,
                target.id,
                package_version_id,
                jobs_created_on_or_after=skip_jobs_not_before,
            ):
                skip_slots += 1
                continue
            expected_jobs += 1
            job = BuildJob(
                package_id=package_id,
                release_id=chain.release_id,
                target_id=target.id,
                package_version_id=package_version_id,
                status=BuildStatus.PENDING,
                priority=priority,
                triggered_by_id=triggered_by_id,
                branch=publish_branch,
                chain_id=chain.id,
                chain_stage_index=stage_index,
            )
            db.add(job)
            await db.flush()
            jobs_created.append(job)
            if triggered_by_id is not None:
                await log_audit(
                    db,
                    "build.trigger",
                    account_id=triggered_by_id,
                    resource_type="build_job",
                    resource_id=str(job.id),
                    details=f"chain_id={chain.id} stage={stage_index} package_id={package_id}",
                    ip_address=get_client_ip(request) if request is not None else None,
                    user_agent=get_user_agent(request) if request is not None else "chain-build",
                )

    chain.stage_expected_jobs = expected_jobs
    chain.stage_skip_slots = skip_slots
    chain.stage_publish_done = 0

    if expected_jobs == 0:
        if skip_slots == 0:
            await fail_chain(
                db,
                chain,
                "No jobs queued for this chain stage (no workers for selected arches or empty stage).",
                terminal=BuildChainStatus.FAILED,
            )
        else:
            await _advance_chain_after_stage_complete(db, chain)

    return jobs_created


async def trigger_chain_build(
    db: Any,
    request: Request,
    user: "Account",
    *,
    chain_spec: str,
    release_id: int,
    architectures: list[str] | None,
    skip_complete_arches: bool,
    preferred_mock_environment_id: int | None,
    distgit_url: str | None,
    distgit_branch: str | None,
    priority: int,
) -> tuple[BuildChain, list[BuildJob]]:
    stages = parse_chain_build_spec(chain_spec)
    rr = await db.execute(select(Release).where(Release.id == release_id))
    release = rr.scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")

    if preferred_mock_environment_id:
        me_r = await db.execute(
            select(MockEnvironment).where(MockEnvironment.id == preferred_mock_environment_id)
        )
        me = me_r.scalar_one_or_none()
        if not me or me.release_id != release_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Mock environment not found or does not belong to the selected release.",
            )

    arches = architectures or DEFAULT_ARCH
    arches = [(a or "").strip() for a in arches if (a or "").strip()]
    if not arches:
        arches = DEFAULT_ARCH

    workers_result = await db.execute(select(Worker))
    workers = list(workers_result.scalars().all())
    workers_by_arch: dict[str, bool] = {}
    for w in workers:
        workers_by_arch[normalize_machine_arch(w.architecture)] = True
    saw_worker = any(workers_by_arch.get(normalize_machine_arch(a), False) for a in arches)
    if not saw_worker:
        req_norm = sorted({normalize_machine_arch(a) for a in arches})
        avail_norm = sorted(workers_by_arch.keys())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "No worker enrolled for any selected architecture. "
                f"Requested: {', '.join(req_norm)}. "
                f"Workers cover: {', '.join(avail_norm) if avail_norm else '(no workers)'}. "
            ),
        )

    dg_url = (distgit_url or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    dg_branch = (distgit_branch or "main").strip()

    chain_options: dict[str, Any] = {
        "architectures": arches,
        "skip_complete_arches": skip_complete_arches,
        "distgit_url": dg_url,
        "distgit_branch": dg_branch,
        "preferred_mock_environment_id": preferred_mock_environment_id,
        "priority": priority,
        # Only successes from jobs created on/after this instant count toward
        # skip_complete_arches for this chain (avoids stale all-arch skips).
        "skip_jobs_not_before": datetime.now(timezone.utc).isoformat(),
    }
    chain = BuildChain(
        release_id=release_id,
        triggered_by_id=user.id,
        status=BuildChainStatus.PENDING,
        chain_spec=chain_spec.strip(),
        chain_options=chain_options,
        stages_json=stages,
        active_stage_index=0,
    )
    db.add(chain)
    await db.flush()

    if _chain_use_distgit_dep_gate():
        ok = await _inject_missing_dep_layers_before(db, chain, 0)
        if not ok:
            await db.flush()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=chain.error_message or "Chain build could not resolve dependencies",
            )

    await log_audit(
        db,
        "build.chain_trigger",
        account_id=user.id,
        resource_type="build_chain",
        resource_id=str(chain.id),
        details=chain_spec.strip()[:512],
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )

    jobs = await _enqueue_chain_stage_jobs(
        db,
        chain,
        0,
        architectures=arches,
        skip_complete_arches=skip_complete_arches,
        distgit_url=dg_url,
        distgit_branch=dg_branch,
        preferred_mock_environment_id=preferred_mock_environment_id,
        priority=priority,
        triggered_by_id=user.id,
        request=request,
    )

    if chain.status == BuildChainStatus.FAILED:
        await db.flush()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=chain.error_message or "Chain build could not start",
        )

    return chain, jobs


async def advance_settled_chains() -> None:
    """Advance any chains whose settle window has passed.

    Called by a background loop in main.py. Handles the case where
    OREON_CHAIN_STAGE_SETTLE_SEC > 0 and the settle period has elapsed.
    Also recovers chains whose advance was deferred across an API restart.
    """
    from oreon_build.db.session import async_session_maker

    async def _reconcile_stage_publish_done_from_publish_tasks(db: Any, chain: BuildChain) -> None:
        """
        Strict recovery for missed callbacks.

        Count current-stage jobs whose publish task already succeeded, then sync
        `stage_publish_done`. This is still fully publish-gated (no bypass).
        """
        if chain.stage_expected_jobs <= 0:
            return
        stage_job_ids = list(
            (
                await db.execute(
                    select(BuildJob.id).where(
                        BuildJob.chain_id == chain.id,
                        BuildJob.chain_stage_index == chain.active_stage_index,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not stage_job_ids:
            return
        done_job_ids = list(
            (
                await db.execute(
                    select(BuildAttempt.build_job_id)
                    .join(PublishTask, PublishTask.attempt_id == BuildAttempt.id)
                    .where(
                        BuildAttempt.build_job_id.in_(stage_job_ids),
                        PublishTask.status == "success",
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        done = len(done_job_ids)
        if done > chain.stage_publish_done:
            logger.info(
                "chain_id=%s stage=%s reconciled publish progress %s -> %s",
                chain.id,
                chain.active_stage_index,
                chain.stage_publish_done,
                done,
            )
            chain.stage_publish_done = done

    now = datetime.now(timezone.utc)
    async with async_session_maker() as db:
        result = await db.execute(
            select(BuildChain)
            .where(
                BuildChain.status.in_([BuildChainStatus.PENDING, BuildChainStatus.RUNNING]),
                BuildChain.stage_expected_jobs > 0,
            )
            .with_for_update(skip_locked=True)
        )
        chains = list(result.scalars().all())
        if not chains:
            return
        for chain in chains:
            await _reconcile_stage_publish_done_from_publish_tasks(db, chain)
            if chain.stage_publish_done < chain.stage_expected_jobs:
                continue
            if chain.stage_advance_after is None:
                settle = _chain_stage_settle_sec()
                if settle > 0:
                    chain.stage_advance_after = datetime.now(timezone.utc) + timedelta(seconds=settle)
                    logger.info(
                        "chain_id=%s stage=%s all publishes done; advancing in %ss at %s",
                        chain.id,
                        chain.active_stage_index,
                        settle,
                        chain.stage_advance_after.isoformat(),
                    )
                    continue
                await _advance_chain_after_stage_complete(db, chain)
                continue
            if chain.stage_advance_after > now:
                continue
            logger.info(
                "chain_id=%s stage=%s settle elapsed, advancing to next stage",
                chain.id,
                chain.active_stage_index,
            )
            try:
                await _advance_chain_after_stage_complete(db, chain)
            except Exception:
                logger.exception("chain_id=%s advance failed", chain.id)
        await db.commit()


async def resume_chain_from_previous(
    db: Any,
    request: Request,
    user: "Account",
    *,
    from_chain_id: int,
) -> tuple[BuildChain, list[BuildJob]]:
    """Start a new chain with the same spec and options as a previous (failed or finished) chain.

    Uses ``skip_complete_arches`` from the previous options so already-built packages are skipped.
    """
    old_r = await db.execute(select(BuildChain).where(BuildChain.id == from_chain_id))
    old = old_r.scalar_one_or_none()
    if not old:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chain not found")
    spec = (old.chain_spec or "").strip()
    if not spec:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source chain has no chain_spec")
    opt = _options_dict(old)
    opt.setdefault("skip_complete_arches", True)
    _pm = opt.get("preferred_mock_environment_id")
    if _pm is not None and _pm != "":
        try:
            _pm = int(_pm)  # JSON may have stored a string
        except (TypeError, ValueError):
            _pm = None
    else:
        _pm = None
    return await trigger_chain_build(
        db,
        request,
        user,
        chain_spec=spec,
        release_id=old.release_id,
        architectures=list(opt.get("architectures") or DEFAULT_ARCH),
        skip_complete_arches=bool(opt.get("skip_complete_arches", True)),
        preferred_mock_environment_id=_pm,
        distgit_url=opt.get("distgit_url"),  # type: ignore[arg-type]
        distgit_branch=opt.get("distgit_branch"),  # type: ignore[arg-type]
        priority=int(opt.get("priority") or 0),
    )


async def notify_chain_job_terminal_after_result(
    build_job_id: int,
    status_str: str,
    error_message: str | None,
) -> None:
    st = (status_str or "").lower()
    if st not in ("failed", "cancelled"):
        return
    from oreon_build.db.session import async_session_maker

    reason = (error_message or "").strip() or f"Build {st}"
    async with async_session_maker() as db:
        job = await db.get(BuildJob, build_job_id)
        if job is None or job.chain_id is None:
            return
        await on_build_job_failed_for_chain(db, job, reason)
        await db.commit()
