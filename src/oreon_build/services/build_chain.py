from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.chain_build_spec import parse_chain_build_spec
from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.core.repo_branch import r2_publish_branch, release_distgit_branch
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
from oreon_build.services.distgit_order import db_safe_spec_evr
from oreon_build.services.build_target_util import (
    get_or_create_build_target,
    latest_package_version_id,
)

if TYPE_CHECKING:
    from oreon_build.models import Account

logger = logging.getLogger(__name__)

DEFAULT_ARCH = ["x86_64", "aarch64"]


def _job_build_terminal_ok(status: BuildStatus) -> bool:
    return status in (BuildStatus.SUCCESS, BuildStatus.SKIPPED)


def _chain_stage_settle_sec() -> int:
    raw = (os.environ.get("OREON_CHAIN_STAGE_SETTLE_SEC") or "30").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 30
    return max(0, min(n, 600))


def _options_dict(chain: BuildChain) -> dict[str, Any]:
    raw = chain.chain_options
    return dict(raw) if isinstance(raw, dict) else {}


def _set_chain_options(chain: BuildChain, options: dict[str, Any]) -> None:
    chain.chain_options = options
    flag_modified(chain, "chain_options")


def _stages_list(chain: BuildChain) -> list[list[str]]:
    raw = chain.stages_json if isinstance(chain.stages_json, list) else []
    out: list[list[str]] = []
    for grp in raw:
        if isinstance(grp, list):
            out.append([str(p) for p in grp if p])
    return out


async def _job_has_chain_publish_receipt(db: Any, chain_id: int, job: BuildJob) -> bool:
    att_r = await db.execute(
        select(BuildAttempt.id).where(
            BuildAttempt.build_job_id == job.id,
            BuildAttempt.status == BuildStatus.SUCCESS,
        )
    )
    attempt_ids = [int(a) for a in att_r.scalars().all()]
    if not attempt_ids:
        return False
    rec_r = await db.execute(
        select(BuildChainPublishReceipt.id)
        .where(
            BuildChainPublishReceipt.build_chain_id == chain_id,
            BuildChainPublishReceipt.build_attempt_id.in_(attempt_ids),
        )
        .limit(1)
    )
    return rec_r.scalar_one_or_none() is not None


def first_incomplete_stage_index_from_states(
    stage_count: int,
    jobs_by_stage: dict[int, list[tuple[bool, bool]]],
) -> int:
    for idx in range(stage_count):
        jobs = jobs_by_stage.get(idx, [])
        if not jobs:
            return idx
        if not all(build_ok and published for build_ok, published in jobs):
            return idx
    return stage_count


async def _first_incomplete_stage_index(db: Any, chain: BuildChain) -> int:
    stages = _stages_list(chain)
    if not stages:
        return 0
    jobs_r = await db.execute(select(BuildJob).where(BuildJob.chain_id == chain.id))
    by_stage: dict[int, list[tuple[bool, bool]]] = {}
    for job in jobs_r.scalars().all():
        if job.chain_stage_index is None:
            continue
        idx = int(job.chain_stage_index)
        build_ok = _job_build_terminal_ok(job.status)
        published = build_ok and (
            job.status == BuildStatus.SKIPPED
            or await _job_has_chain_publish_receipt(db, chain.id, job)
        )
        by_stage.setdefault(idx, []).append((build_ok, published))
    return first_incomplete_stage_index_from_states(len(stages), by_stage)


async def _old_chain_success_slots(
    db: Any,
    chain_id: int,
    stage_index: int,
) -> set[tuple[int, int]]:
    jobs_r = await db.execute(
        select(BuildJob).where(
            BuildJob.chain_id == chain_id,
            BuildJob.chain_stage_index == stage_index,
            BuildJob.status.in_((BuildStatus.SUCCESS, BuildStatus.SKIPPED)),
        )
    )
    out: set[tuple[int, int]] = set()
    for job in jobs_r.scalars().all():
        if job.package_id is None or job.target_id is None:
            continue
        if await _job_has_chain_publish_receipt(db, chain_id, job):
            out.add((int(job.package_id), int(job.target_id)))
    return out


def distgit_new_package_dict(
    pkg: str,
    releasename: str,
    *,
    distgit_url: str,
    distgit_branch: str,
    preferred_mock_environment_id: int | None,
    spec_version: str | None = None,
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
        "version": db_safe_spec_evr((spec_version or "0.0.1").strip() or "0.0.1"),
    }


async def _ensure_spec_package_version(
    db: Any,
    package_id: int,
    spec_canonical: str | None,
) -> int | None:
    if not spec_canonical:
        return await latest_package_version_id(db, package_id)
    spec_canonical = db_safe_spec_evr(spec_canonical)
    row = await db.execute(
        select(PackageVersion.id)
        .where(
            PackageVersion.package_id == package_id,
            PackageVersion.version == spec_canonical,
        )
        .order_by(PackageVersion.id.desc())
        .limit(1)
    )
    vid = row.scalar_one_or_none()
    if vid is not None:
        return int(vid)
    pv = PackageVersion(package_id=package_id, version=spec_canonical)
    db.add(pv)
    await db.flush()
    return pv.id


async def _ensure_distgit_source_on_package_version(
    db: Any,
    package_version_id: int | None,
    *,
    pkg_name: str,
    releasename: str,
    distgit_url: str,
    distgit_branch: str,
) -> None:
    if package_version_id is None or not releasename.strip():
        return
    want_url = distgit_new_package_dict(
        pkg_name,
        releasename,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        preferred_mock_environment_id=None,
    )["source_url"]
    row = await db.execute(
        select(Source)
        .where(
            Source.package_version_id == package_version_id,
            Source.kind == "distgit",
        )
        .limit(1)
    )
    src = row.scalar_one_or_none()
    if src is None:
        db.add(Source(package_version_id=package_version_id, kind="distgit", url=want_url))
        await db.flush()
        return
    if (src.url or "").strip() != want_url:
        src.url = want_url
        await db.flush()


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
    if job.status in (BuildStatus.SUCCESS, BuildStatus.SKIPPED, BuildStatus.FAILED, BuildStatus.CANCELLED):
        return
    job.status = BuildStatus.CANCELLED
    att_r = await db.execute(
        select(BuildAttempt)
        .where(
            BuildAttempt.build_job_id == job.id,
            BuildAttempt.status.in_((BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)),
        )
    )
    for att in att_r.scalars().all():
        att.status = BuildStatus.CANCELLED
        if not (att.error_message or "").strip():
            att.error_message = note


async def fail_chain(
    db: Any,
    chain: BuildChain,
    message: str,
    *,
    terminal: BuildChainStatus = BuildChainStatus.FAILED,
) -> None:
    chain.status = terminal
    chain.error_message = (message or terminal.value)[:8000]
    chain.stage_advance_after = None
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
    await fail_chain(db, chain, reason, terminal=BuildChainStatus.FAILED)


async def on_user_cancelled_chain_job(db: Any, job: BuildJob) -> None:
    if job.chain_id is None:
        return
    cres = await db.execute(select(BuildChain).where(BuildChain.id == job.chain_id).with_for_update())
    chain = cres.scalar_one_or_none()
    if not chain or chain.status not in (BuildChainStatus.PENDING, BuildChainStatus.RUNNING):
        return
    await fail_chain(db, chain, "Cancelled by user", terminal=BuildChainStatus.CANCELLED)


async def cancel_chain_by_user(
    db: Any,
    request: Request,
    user: "Account",
    *,
    chain_id: int,
) -> BuildChain:
    result = await db.execute(
        select(BuildChain).where(BuildChain.id == chain_id).with_for_update()
    )
    chain = result.scalar_one_or_none()
    if not chain:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chain not found")
    if chain.status in (
        BuildChainStatus.SUCCESS,
        BuildChainStatus.FAILED,
        BuildChainStatus.CANCELLED,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Chain is already {chain.status.value}",
        )
    await fail_chain(db, chain, "Cancelled by user", terminal=BuildChainStatus.CANCELLED)
    await log_audit(
        db,
        "build.chain_cancel",
        account_id=user.id,
        resource_type="build_chain",
        resource_id=str(chain.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return chain


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
    distgit_url: str,
    distgit_branch: str,
    preferred_mock_environment_id: int | None,
    priority: int,
    triggered_by_id: int | None,
    request: Request | None,
    skip_package_target_slots: set[tuple[int, int]] | None = None,
    advance_if_stage_satisfied_by_skips: bool = False,
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
    skip_slots_set = skip_package_target_slots or set()

    for pkg_name in pkgs:
        existing_r = await db.execute(select(Package).where(Package.name == pkg_name))
        existing_pkg = existing_r.scalar_one_or_none()
        package_id: int
        package_version_id: int | None
        pref_mock: int | None

        if existing_pkg:
            package_id = existing_pkg.id
            package_version_id = await _ensure_spec_package_version(db, package_id, None)
            if releasename:
                await _ensure_distgit_source_on_package_version(
                    db,
                    package_version_id,
                    pkg_name=pkg_name,
                    releasename=releasename,
                    distgit_url=distgit_url,
                    distgit_branch=distgit_branch,
                )
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
            if (package_id, target.id) in skip_slots_set:
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
        if skip_slots > 0 and advance_if_stage_satisfied_by_skips:
            await _advance_chain_after_stage_complete(db, chain)
        else:
            await fail_chain(
                db,
                chain,
                "No jobs queued for this chain stage (no workers for selected arches or empty stage).",
                terminal=BuildChainStatus.FAILED,
            )

    return jobs_created


async def trigger_chain_build(
    db: Any,
    request: Request,
    user: "Account",
    *,
    chain_spec: str,
    release_id: int,
    architectures: list[str] | None,
    preferred_mock_environment_id: int | None,
    distgit_url: str | None,
    distgit_branch: str | None,
    priority: int,
    defer_enqueue: bool = False,
) -> tuple[BuildChain, list[BuildJob], bool]:
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
    dg_branch = (distgit_branch or release_distgit_branch(release)).strip()

    chain_options: dict[str, Any] = {
        "architectures": arches,
        "distgit_url": dg_url,
        "distgit_branch": dg_branch,
        "preferred_mock_environment_id": preferred_mock_environment_id,
        "priority": priority,
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

    jobs: list[BuildJob] = []
    if not defer_enqueue:
        jobs = await _enqueue_chain_stage_jobs(
            db,
            chain,
            0,
            architectures=arches,
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

    return chain, jobs, defer_enqueue


async def enqueue_chain_jobs_after_create(db: Any, chain_id: int) -> None:
    chain_r = await db.execute(select(BuildChain).where(BuildChain.id == chain_id).with_for_update())
    chain = chain_r.scalar_one_or_none()
    if not chain:
        logger.error("chain enqueue: chain_id=%s not found", chain_id)
        return
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
    dg_url = str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    dg_branch = str(opt.get("distgit_branch") or "main").strip()
    await _enqueue_chain_stage_jobs(
        db,
        chain,
        0,
        architectures=arches,
        distgit_url=dg_url,
        distgit_branch=dg_branch,
        preferred_mock_environment_id=pref_pm,
        priority=int(opt.get("priority") or 0),
        triggered_by_id=chain.triggered_by_id,
        request=None,
    )
    if chain.status == BuildChainStatus.FAILED:
        logger.warning(
            "chain_id=%s failed during background enqueue: %s",
            chain_id,
            chain.error_message,
        )


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
                chain.stage_advance_after = datetime.now(timezone.utc) + timedelta(seconds=settle)
                logger.info(
                    "chain_id=%s stage=%s all publishes done; advancing in %ss",
                    chain.id,
                    chain.active_stage_index,
                    settle,
                )
            else:
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
        await fail_chain(db, chain, err or "Repo publish failed", terminal=BuildChainStatus.FAILED)
        await db.commit()


async def advance_settled_chains() -> None:
    from oreon_build.db.session import async_session_maker

    async with async_session_maker() as db:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(BuildChain)
            .where(
                BuildChain.status.in_((BuildChainStatus.PENDING, BuildChainStatus.RUNNING)),
                BuildChain.stage_advance_after.isnot(None),
                BuildChain.stage_advance_after <= now,
            )
            .with_for_update(skip_locked=True)
        )
        chains = list(result.scalars().all())
        for chain in chains:
            chain.stage_advance_after = None
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
    defer_enqueue: bool = False,
) -> tuple[BuildChain, list[BuildJob], bool]:
    old_r = await db.execute(select(BuildChain).where(BuildChain.id == from_chain_id))
    old = old_r.scalar_one_or_none()
    if not old:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chain not found")
    if old.status in (BuildChainStatus.PENDING, BuildChainStatus.RUNNING):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That chain is still running. Cancel it or wait for it to finish before resuming.",
        )
    spec = (old.chain_spec or "").strip()
    if not spec:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source chain has no chain_spec")
    stages = _stages_list(old)
    if not stages:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source chain has no stages")

    resume_stage = await _first_incomplete_stage_index(db, old)
    active_idx = int(old.active_stage_index or 0)
    if old.status in (BuildChainStatus.FAILED, BuildChainStatus.CANCELLED):
        resume_stage = max(resume_stage, active_idx)
    if resume_stage >= len(stages):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That chain already finished every stage successfully.",
        )

    opt = _options_dict(old)
    _pm = opt.get("preferred_mock_environment_id")
    if _pm is not None and _pm != "":
        try:
            _pm = int(_pm)
        except (TypeError, ValueError):
            _pm = None
    else:
        _pm = None

    rr = await db.execute(select(Release).where(Release.id == old.release_id))
    release = rr.scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")

    if _pm:
        me_r = await db.execute(select(MockEnvironment).where(MockEnvironment.id == _pm))
        me = me_r.scalar_one_or_none()
        if not me or me.release_id != old.release_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Mock environment not found or does not belong to the selected release.",
            )

    arches = list(opt.get("architectures") or DEFAULT_ARCH)
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

    dg_url = str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    dg_branch = str(opt.get("distgit_branch") or "main").strip()
    priority = int(opt.get("priority") or 0)
    resume_start = resume_stage

    chain_options: dict[str, Any] = {
        "architectures": arches,
        "distgit_url": dg_url,
        "distgit_branch": dg_branch,
        "preferred_mock_environment_id": _pm,
        "priority": priority,
        "resumed_from_chain_id": from_chain_id,
        "resume_start_stage_index": resume_start,
    }
    chain = BuildChain(
        release_id=old.release_id,
        triggered_by_id=user.id,
        status=BuildChainStatus.PENDING,
        chain_spec=spec,
        chain_options=chain_options,
        stages_json=stages,
        active_stage_index=resume_stage,
    )
    db.add(chain)
    await db.flush()

    await log_audit(
        db,
        "build.chain_resume",
        account_id=user.id,
        resource_type="build_chain",
        resource_id=str(chain.id),
        details=f"from_chain_id={from_chain_id} stage={resume_stage}",
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )

    if defer_enqueue:
        return chain, [], True

    await enqueue_resumed_chain_stages(db, chain.id, from_chain_id)
    if chain.status == BuildChainStatus.FAILED:
        await db.flush()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=chain.error_message or "Chain resume could not start",
        )
    jobs_r = await db.execute(select(BuildJob).where(BuildJob.chain_id == chain.id))
    return chain, list(jobs_r.scalars().all()), False


async def enqueue_resumed_chain_stages(db: Any, chain_id: int, from_chain_id: int) -> None:
    chain_r = await db.execute(select(BuildChain).where(BuildChain.id == chain_id).with_for_update())
    chain = chain_r.scalar_one_or_none()
    if not chain:
        logger.error("chain resume enqueue: chain_id=%s not found", chain_id)
        return
    stages = _stages_list(chain)
    opt = _options_dict(chain)
    resume_start = int(opt.get("resume_start_stage_index") or chain.active_stage_index or 0)
    arches = [str(x) for x in opt.get("architectures") or DEFAULT_ARCH]
    _pm = opt.get("preferred_mock_environment_id")
    if _pm is None or _pm == "":
        pref_pm = None
    else:
        try:
            pref_pm = int(_pm)
        except (TypeError, ValueError):
            pref_pm = None
    dg_url = str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    dg_branch = str(opt.get("distgit_branch") or "main").strip()
    priority = int(opt.get("priority") or 0)

    stage = resume_start
    while stage < len(stages) and chain.status not in (
        BuildChainStatus.FAILED,
        BuildChainStatus.SUCCESS,
        BuildChainStatus.CANCELLED,
    ):
        skip_set = await _old_chain_success_slots(db, from_chain_id, stage) if stage == resume_start else set()
        await _enqueue_chain_stage_jobs(
            db,
            chain,
            stage,
            architectures=arches,
            distgit_url=dg_url,
            distgit_branch=dg_branch,
            preferred_mock_environment_id=pref_pm,
            priority=priority,
            triggered_by_id=chain.triggered_by_id,
            request=None,
            skip_package_target_slots=skip_set,
            advance_if_stage_satisfied_by_skips=True,
        )
        if chain.status in (BuildChainStatus.FAILED, BuildChainStatus.SUCCESS, BuildChainStatus.CANCELLED):
            break
        if chain.active_stage_index > stage:
            if chain.status == BuildChainStatus.SUCCESS or chain.stage_expected_jobs > 0:
                break
            stage = chain.active_stage_index
            continue
        break
    if chain.status == BuildChainStatus.FAILED:
        logger.warning(
            "chain_id=%s resume failed during background enqueue: %s",
            chain_id,
            chain.error_message,
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
