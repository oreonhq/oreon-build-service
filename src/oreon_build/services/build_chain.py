"""Manual chain builds: user-defined stages, publish-gated advance, no auto dependency injection."""
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
from oreon_build.services.distgit_order import db_safe_spec_evr
from oreon_build.services.build_target_util import (
    batch_package_ids_by_name,
    get_or_create_build_target,
    latest_package_version_id,
    package_ids_with_success_on_all_targets,
)

if TYPE_CHECKING:
    from oreon_build.models import Account

logger = logging.getLogger(__name__)

DEFAULT_ARCH = ["x86_64", "aarch64"]


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


async def _chain_build_target_ids(
    db: Any,
    chain: BuildChain,
    *,
    architectures: list[str],
    preferred_mock_environment_id: int | None,
) -> list[int]:
    target_ids: list[int] = []
    for arch in architectures:
        canon = normalize_machine_arch(arch)
        target = await get_or_create_build_target(
            db, chain.release_id, canon, preferred_mock_environment_id
        )
        target_ids.append(target.id)
    return target_ids


async def _package_ids_still_needing_chain_build(
    db: Any,
    chain: BuildChain,
    package_ids: list[int],
    *,
    architectures: list[str],
    preferred_mock_environment_id: int | None,
    version_by_package: dict[int, int | None],
    jobs_created_on_or_after: datetime | None = None,
) -> set[int]:
    ids = [int(p) for p in package_ids if p]
    if not ids or not architectures:
        return set(ids)
    target_ids = await _chain_build_target_ids(
        db,
        chain,
        architectures=architectures,
        preferred_mock_environment_id=preferred_mock_environment_id,
    )
    complete_ids = await package_ids_with_success_on_all_targets(
        db,
        package_ids=ids,
        release_id=chain.release_id,
        target_ids=target_ids,
        version_by_package=version_by_package,
        jobs_created_on_or_after=jobs_created_on_or_after,
    )
    return {pid for pid in ids if pid not in complete_ids}


async def _batch_chain_packages_still_needing_build(
    db: Any,
    chain: BuildChain,
    package_names: list[str],
    *,
    architectures: list[str],
    preferred_mock_environment_id: int | None,
) -> set[str]:
    """Package names that still need a chain build (DB success on all arches since chain start)."""
    still: set[str] = set()
    to_lookup = [n for n in package_names if n]
    if not to_lookup:
        return still

    name_to_id = await batch_package_ids_by_name(db, to_lookup)
    chain_start = _parse_skip_jobs_not_before(chain)

    check_ids: list[int] = []
    id_to_name: dict[int, str] = {}
    version_by_package: dict[int, int | None] = {}

    for name in to_lookup:
        if name not in name_to_id:
            still.add(name)
            continue
        pid = name_to_id[name]
        pv_id = await latest_package_version_id(db, pid)
        version_by_package[pid] = pv_id
        check_ids.append(pid)
        id_to_name[pid] = name

    if not check_ids:
        return still

    lacking_ids = await _package_ids_still_needing_chain_build(
        db,
        chain,
        check_ids,
        architectures=architectures,
        preferred_mock_environment_id=preferred_mock_environment_id,
        version_by_package=version_by_package,
        jobs_created_on_or_after=chain_start,
    )
    for pid in lacking_ids:
        still.add(id_to_name[pid])
    return still


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
    if job.status in (BuildStatus.SUCCESS, BuildStatus.FAILED, BuildStatus.CANCELLED):
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

    still_need_names: set[str] = set()
    if skip_complete_arches:
        still_need_names = await _batch_chain_packages_still_needing_build(
            db,
            chain,
            pkgs,
            architectures=architectures,
            preferred_mock_environment_id=preferred_mock_environment_id,
        )

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
            if skip_complete_arches and pkg_name not in still_need_names:
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
    dg_branch = (distgit_branch or "main").strip()

    chain_options: dict[str, Any] = {
        "architectures": arches,
        "skip_complete_arches": skip_complete_arches,
        "distgit_url": dg_url,
        "distgit_branch": dg_branch,
        "preferred_mock_environment_id": preferred_mock_environment_id,
        "priority": priority,
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

    return chain, jobs, False


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
) -> tuple[BuildChain, list[BuildJob], bool]:
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
            _pm = int(_pm)
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
        distgit_url=opt.get("distgit_url"),
        distgit_branch=opt.get("distgit_branch"),
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
