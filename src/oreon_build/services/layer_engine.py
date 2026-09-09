"""Native-only layer scheduler.

Jobs build when native libsolv says the worker BRs are installable. Missing
caps enqueue provider sources from the Provides catalog. No Fedora chroot,
no stored dep DAG, no spec parse.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Iterable

from fastapi import HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.dnf_missing import parse_dnf_missing_provides
from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.core.repo_branch import release_distgit_branch
from oreon_build.core.rpm_requires import is_distgit_source_name
from oreon_build.models import (
    BuildJob,
    BuildStatus,
    LayerJobBlocker,
    LayerPlan,
    LayerPlanStatus,
    MockEnvironment,
    Package,
    Release,
    SideTag,
)
from oreon_build.services.build_pass import job_options_dict, schedule_next_pass_after_publish
from oreon_build.services.build_target_util import get_or_create_build_target, resolve_mock_environment_id
from oreon_build.services.distgit_order import _github_owner_repo_from_git_url
from oreon_build.services.layer_plan import get_or_create_package
from oreon_build.services.repo_solver import get_repo_solver, invalidate_repo_solver_cache

if TYPE_CHECKING:
    from oreon_build.models import Account

logger = logging.getLogger(__name__)
DEFAULT_ARCH = ["x86_64", "aarch64"]
PHASE1_BRIDGE = "phase1_bridge"
PHASE2_NATIVE = "phase2_native"
NATIVE_PHASE = PHASE1_BRIDGE
ACTIVE = (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.BLOCKED, BuildStatus.RUNNING)
EXECUTING = (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)
_LAYER_JOB_WRITABLE = frozenset((BuildStatus.QUEUED, BuildStatus.BLOCKED))
_MAX_SOURCES_PER_CAP = 4


def clear_reactive_solver_cache() -> None:
    pass


def invalidate_reactive_requirements_for_package(package_name: str) -> None:
    pass


def parse_missing_provides_from_text(text: str) -> set[str]:
    return set(parse_dnf_missing_provides(text))


def _options(plan: LayerPlan) -> dict[str, Any]:
    return dict(plan.plan_options) if isinstance(plan.plan_options, dict) else {}


def _requirements(job: BuildJob) -> tuple[str, ...] | None:
    raw = getattr(job, "source_requirements", None)
    if raw is None:
        options = job_options_dict(job)
        raw = options.get("solver_requirements") or options.get("sat_hold_caps") or ()
    requirements = tuple(sorted({str(x).strip() for x in (raw or ()) if str(x).strip()}))
    return requirements or None


def _built_on_fallback(job: BuildJob) -> bool:
    return False


def _arch_config(config_name: str, arch: str) -> str:
    base = str(config_name or "").strip()
    if not base:
        return ""
    for suffix in ("-x86_64", "-aarch64"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    return f"{base}-{arch}"


def _job_arch(job: BuildJob) -> str:
    return normalize_machine_arch(str(job_options_dict(job).get("architecture") or "x86_64"))


def _job_pkg_name(job: BuildJob) -> str:
    pkg = getattr(job, "package", None)
    name = getattr(pkg, "name", None) if pkg is not None else None
    return str(name or "").strip()


def _native_config(job: BuildJob, arch: str | None = None) -> str:
    arch = arch or _job_arch(job)
    return str(job_options_dict(job).get("native_mock_config") or f"oreon-11-{arch}")


def _missing_caps(job: BuildJob) -> list[str]:
    opts = job_options_dict(job)
    raw = opts.get("missing_caps")
    if isinstance(raw, list) and raw:
        return [str(x).strip() for x in raw if str(x).strip()]
    reason = str(job.blocked_reason or "")
    if "waiting on repository:" in reason:
        tail = reason.split("waiting on repository:", 1)[1]
        return [p.strip() for p in tail.split(",") if p.strip()]
    return list(_requirements(job) or ())


def _cap_base(cap: str) -> str:
    return str(cap or "").strip().split()[0].split("(")[0].strip()


def pick_provider_sources(cap: str, names: Iterable[str]) -> list[str]:
    found = {str(n).strip() for n in names if is_distgit_source_name(str(n).strip())}
    if not found:
        return []
    base = _cap_base(cap)
    if base in found:
        return [base]
    return sorted(found)[:_MAX_SOURCES_PER_CAP]


def _native_solver(releasename: str, branch: str, arch: str) -> Any:
    return get_repo_solver(releasename, branch, [arch])


def _catalog_solver(releasename: str, branch: str, arch: str) -> Any:
    from oreon_build.services.repo_solver import get_provides_catalog_solver

    return get_provides_catalog_solver(releasename, branch, arch)


async def native_missing_caps(job: BuildJob, releasename: str, side_tag_repo_path: str, requirements: Iterable[str]) -> list[str]:
    reqs = tuple(sorted({str(x).strip() for x in requirements if str(x).strip()}))
    if not reqs:
        return []
    arch = _job_arch(job)
    branch = os.path.basename(os.path.normpath(side_tag_repo_path)) or str(job.branch or "dev")

    def solve() -> Any:
        return _native_solver(releasename, branch, arch).resolve(reqs)

    native = await asyncio.to_thread(solve)
    if native.ok:
        return []
    return sorted(native.missing_provides or ())


async def probe_job_target(job: BuildJob, releasename: str, side_tag_repo_path: str) -> str:
    options = job_options_dict(job)
    arch = _job_arch(job)
    branch = os.path.basename(os.path.normpath(side_tag_repo_path)) or str(job.branch or "dev")
    native_config = _native_config(job, arch)
    job.target_mock_config = native_config
    requirements = _requirements(job)
    if requirements is None:
        job.status = BuildStatus.QUEUED
        job.blocked_reason = None
        options.pop("missing_caps", None)
        job.job_options = options
        return native_config

    def solve() -> Any:
        return _native_solver(releasename, branch, arch).resolve(requirements)

    native = await asyncio.to_thread(solve)
    if native.ok:
        job.status = BuildStatus.QUEUED
        job.blocked_reason = None
        options.pop("missing_caps", None)
        job.job_options = options
        job.blocking_sources = []
        return native_config

    missing = sorted(native.missing_provides or ())
    options["missing_caps"] = missing
    job.job_options = options
    job.status = BuildStatus.BLOCKED
    job.blocked_reason = "waiting on repository: " + ", ".join(missing)
    return native_config


async def create_pending_layer_plan(
    db: Any, request: Request | None, user: "Account", *, root_packages: list[str],
    release_id: int, side_tag_id: int, architectures: list[str] | None,
    preferred_mock_environment_id: int | None, distgit_url: str | None = None,
    distgit_branch: str | None, priority: int,
) -> LayerPlan:
    from oreon_build.services.side_tags import bump_side_tag_activity, resolve_required_side_tag
    roots = sorted({str(x).strip() for x in root_packages if str(x).strip()})
    if not roots:
        raise HTTPException(status_code=400, detail="No packages given")
    release = await db.get(Release, release_id)
    if release is None:
        raise HTTPException(status_code=404, detail="Release not found")
    tag = await resolve_required_side_tag(
        db, release_id=release_id, side_tag_id=side_tag_id, user=user, require_access=True
    )
    if preferred_mock_environment_id is not None:
        env = await db.get(MockEnvironment, preferred_mock_environment_id)
        if env is None or env.release_id != release_id:
            raise HTTPException(status_code=400, detail="Mock environment does not belong to release")
    url = (distgit_url or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    if not _github_owner_repo_from_git_url(url):
        raise HTTPException(status_code=400, detail="Unsupported DistGit URL")
    plan = LayerPlan(
        release_id=release_id, triggered_by_id=user.id, status=LayerPlanStatus.PENDING,
        root_packages=roots, side_tag_id=tag.id,
        plan_options={
            "architectures": list(architectures or DEFAULT_ARCH), "distgit_url": url,
            "distgit_branch": (distgit_branch or release_distgit_branch(release)).strip(),
            "preferred_mock_environment_id": preferred_mock_environment_id,
            "priority": int(priority or 0),
            "current_wave": 0,
        },
    )
    db.add(plan)
    await db.flush()
    await bump_side_tag_activity(db, tag.id)
    await log_audit(
        db, "build.layer_trigger", account_id=user.id, resource_type="layer_plan",
        resource_id=str(plan.id), details=" ".join(roots)[:512],
        ip_address=get_client_ip(request) if request else None,
        user_agent=get_user_agent(request) if request else "layer-build",
    )
    return plan


async def _new_job(db: Any, plan: LayerPlan, release: Release, tag: SideTag, name: str, arch: str) -> BuildJob:
    opts = _options(plan)
    package_id, version_id = await get_or_create_package(
        db, name, release_id=release.id, releasename=release.releasename,
        distgit_url=opts["distgit_url"], distgit_branch=opts["distgit_branch"],
        preferred_mock_environment_id=opts.get("preferred_mock_environment_id"),
        triggered_by_id=plan.triggered_by_id, request=None, layer_plan_ids=[plan.id], skip_audit=True,
    )
    mock_id = resolve_mock_environment_id(
        trigger_mock_environment_id=opts.get("preferred_mock_environment_id"),
        release_default_mock_environment_id=release.default_mock_environment_id,
    )
    target = await get_or_create_build_target(db, release.id, normalize_machine_arch(arch), mock_id)
    native_env = await db.get(MockEnvironment, mock_id) if mock_id is not None else None
    normalized_arch = normalize_machine_arch(arch)
    native_config = _arch_config(native_env.config_name if native_env else "oreon-11", normalized_arch)
    wave = int(opts.get("current_wave") or 0)
    job = BuildJob(
        package_id=package_id, package_version_id=version_id, release_id=release.id,
        target_id=target.id, side_tag_id=tag.id, layer_plan_id=plan.id,
        status=BuildStatus.QUEUED, build_phase=NATIVE_PHASE,
        target_mock_config=native_config,
        priority=int(opts.get("priority") or 0), triggered_by_id=plan.triggered_by_id,
        branch=tag.name, layer_index=wave,
        job_options={
            "architecture": normalized_arch,
            "native_mock_config": native_config,
        },
    )
    job.build_target = target
    db.add(job)
    return job


async def seed_bridge_plan(
    db: Any, package_names: Iterable[str], target_arch: str, releasename: str,
    side_tag_id: int, *, plan: LayerPlan | None = None,
) -> list[BuildJob]:
    if plan is None:
        plan = (await db.execute(select(LayerPlan).where(
            LayerPlan.side_tag_id == side_tag_id,
            LayerPlan.status.in_((LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING)),
        ).order_by(LayerPlan.id.desc()).limit(1))).scalar_one()
    release, tag = await db.get(Release, plan.release_id), await db.get(SideTag, side_tag_id)
    if release is None or tag is None:
        raise ValueError("release or side tag not found")
    jobs = [await _new_job(db, plan, release, tag, name, target_arch)
            for name in sorted({str(x).strip() for x in package_names if str(x).strip()})]
    await db.flush()
    for job in jobs:
        await probe_job_target(job, releasename, tag.name)
    await db.flush()
    return jobs


async def complete_seed_layer_plan(db: Any, plan_id: int, *, headers: dict[str, str] | None = None) -> list[BuildJob]:
    plan = await db.get(LayerPlan, plan_id)
    if plan is None or plan.status != LayerPlanStatus.PENDING:
        return []
    if await db.scalar(select(BuildJob.id).where(BuildJob.layer_plan_id == plan.id).limit(1)):
        return []
    release = await db.get(Release, plan.release_id)
    if release is None or plan.side_tag_id is None:
        await fail_seed(db, plan, "Release or side tag not found")
        return []
    jobs: list[BuildJob] = []
    for arch in _options(plan).get("architectures", DEFAULT_ARCH):
        jobs += await seed_bridge_plan(db, plan.root_packages, arch, release.releasename, plan.side_tag_id, plan=plan)
    plan.status = LayerPlanStatus.RUNNING if jobs else LayerPlanStatus.FAILED
    if not jobs:
        plan.error_message = "No jobs created"
    await db.flush()
    return jobs


async def seed_layer_plan(db: Any, request: Request, user: "Account", **kwargs: Any) -> tuple[LayerPlan, list[BuildJob]]:
    plan = await create_pending_layer_plan(db, request, user, **kwargs)
    return plan, await complete_seed_layer_plan(db, plan.id)


async def fail_seed(db: Any, plan: LayerPlan, message: str) -> None:
    plan.status, plan.error_message = LayerPlanStatus.FAILED, message[:8000]
    await db.flush()


async def _plan_live_names(db: Any, plan_id: int) -> dict[str, set[str]]:
    rows = await db.execute(
        select(Package.name, BuildJob.job_options).where(
            BuildJob.layer_plan_id == plan_id,
            BuildJob.status.notin_((BuildStatus.CANCELLED,)),
        ).join(Package, Package.id == BuildJob.package_id)
    )
    out: dict[str, set[str]] = {}
    for name, opts in rows.all():
        arch = normalize_machine_arch(str((opts or {}).get("architecture") or "x86_64"))
        out.setdefault(arch, set()).add(str(name).strip())
    return out


async def _replace_blockers(db: Any, job: BuildJob, sources: list[str]) -> None:
    if job.id is None:
        await db.flush()
    await db.execute(delete(LayerJobBlocker).where(LayerJobBlocker.build_job_id == job.id))
    for src in sources:
        db.add(LayerJobBlocker(build_job_id=job.id, blocking_source=src))


def _lookup_sources_for_caps(
    catalog: Any,
    native: Any,
    caps: list[str],
    distgit_hits: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    extra = distgit_hits or {}
    out: dict[str, list[str]] = {}
    for cap in caps:
        names: set[str] = set(extra.get(cap) or ())
        try:
            names |= set(catalog.source_rpms_providing(cap) or ())
        except Exception:
            logger.debug("catalog lookup failed for %s", cap, exc_info=True)
        try:
            names |= set(native.source_rpms_providing(cap) or ())
        except Exception:
            logger.debug("native lookup failed for %s", cap, exc_info=True)
        out[cap] = pick_provider_sources(cap, names)
    return out


async def _distgit_sources_for_caps(plan: LayerPlan, release: Release, caps: list[str]) -> dict[str, list[str]]:
    if not caps:
        return {}
    from oreon_build.config import get_settings
    from oreon_build.services.distgit_order import get_distgit_provides_index_cached
    from oreon_build.services.virtual_resolve import distgit_sources_for_virtual_cap

    opts = _options(plan)
    url = str(opts.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git")
    branch = str(opts.get("distgit_branch") or "").strip()
    gr = _github_owner_repo_from_git_url(url)
    if not gr:
        return {}
    owner, repo = gr
    settings = get_settings()
    tok = (getattr(settings, "github_token", None) or "").strip()
    headers = {"Accept": "application/vnd.github+json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        idx = await get_distgit_provides_index_cached(
            owner=owner,
            repo=repo,
            distgit_branch=branch or "main",
            releasename=release.releasename,
            headers=headers,
        )
    except Exception:
        logger.warning("distgit provides lookup failed", exc_info=True)
        return {}
    if idx is None:
        return {}
    known = set(idx.package_names)
    pmap = idx.indexed_providers()
    out: dict[str, list[str]] = {}
    for cap in caps:
        found = distgit_sources_for_virtual_cap(cap, package_names=known, provider_map=pmap)
        out[cap] = pick_provider_sources(cap, found)
    return out


async def expand_blocked_jobs(
    db: Any,
    plan: LayerPlan,
    release: Release,
    tag: SideTag,
    jobs: list[BuildJob],
) -> list[BuildJob]:
    blocked = [j for j in jobs if j.status == BuildStatus.BLOCKED]
    if not blocked:
        return []
    created: list[BuildJob] = []
    by_arch: dict[str, list[BuildJob]] = {}
    for job in blocked:
        by_arch.setdefault(_job_arch(job), []).append(job)
    live = await _plan_live_names(db, plan.id)
    bump_wave = False
    want: dict[str, set[str]] = {}

    def lookup(arch: str, caps: list[str], distgit_hits: dict[str, list[str]]) -> dict[str, list[str]]:
        catalog = _catalog_solver(release.releasename, tag.name, arch)
        native = _native_solver(release.releasename, tag.name, arch)
        return _lookup_sources_for_caps(catalog, native, caps, distgit_hits)

    for arch, group in by_arch.items():
        caps = sorted({c for job in group for c in _missing_caps(job)})
        distgit_hits = await _distgit_sources_for_caps(plan, release, caps)
        cap_map = await asyncio.to_thread(lookup, arch, caps, distgit_hits) if caps else {}
        for job in group:
            self_name = _job_pkg_name(job)
            sources: list[str] = []
            seen: set[str] = set()
            for cap in _missing_caps(job):
                for src in cap_map.get(cap) or ():
                    if src == self_name or src in seen:
                        continue
                    seen.add(src)
                    sources.append(src)
            job.blocking_sources = sources
            await _replace_blockers(db, job, sources)
            if sources:
                job.blocked_reason = "waiting on repository: " + ", ".join(
                    _missing_caps(job)
                ) + " (" + ", ".join(sources) + ")"
            for src in sources:
                if src not in live.get(arch, set()):
                    want.setdefault(arch, set()).add(src)

    if want:
        opts = _options(plan)
        opts["current_wave"] = int(opts.get("current_wave") or 0) + 1
        plan.plan_options = opts
        bump_wave = True
        for arch, names in want.items():
            for name in sorted(names):
                job = await _new_job(db, plan, release, tag, name, arch)
                created.append(job)
                live.setdefault(arch, set()).add(name)
        await db.flush()
        for job in created:
            await probe_job_target(job, release.releasename, tag.name)
    if bump_wave:
        await db.flush()
    await _fail_wait_cycles(db, plan.id)
    return created


async def _fail_wait_cycles(db: Any, plan_id: int) -> int:
    rows = await db.execute(
        select(BuildJob)
        .options(selectinload(BuildJob.package))
        .where(
            BuildJob.layer_plan_id == plan_id,
            BuildJob.status.in_((BuildStatus.BLOCKED, BuildStatus.QUEUED, BuildStatus.RUNNING, BuildStatus.PENDING)),
        )
    )
    jobs = list(rows.scalars().all())
    by_name: dict[str, list[BuildJob]] = {}
    for job in jobs:
        name = _job_pkg_name(job)
        if name:
            by_name.setdefault(name, []).append(job)
    blocked = [j for j in jobs if j.status == BuildStatus.BLOCKED]
    blocked = [j for j in blocked if j.blocking_sources]
    if not blocked:
        return 0
    graph: dict[int, set[str]] = {}
    for job in blocked:
        graph[job.id] = {str(s).strip() for s in (job.blocking_sources or []) if str(s).strip()}
    live_names = set(by_name)
    closed: list[BuildJob] = []
    for job in blocked:
        waits = graph.get(job.id) or set()
        if not waits:
            continue
        if any(src not in live_names for src in waits):
            continue
        if any(
            other.status in EXECUTING
            for src in waits
            for other in by_name.get(src, ())
        ):
            continue
        closed.append(job)
    if not closed:
        return 0
    closed_names = {_job_pkg_name(j) for j in closed if _job_pkg_name(j)}
    closed = [j for j in closed if (graph.get(j.id) or set()) <= closed_names]
    if not closed:
        return 0
    unpublished = False
    for job in jobs:
        if job.status == BuildStatus.SUCCESS:
            opts = job_options_dict(job)
            if opts.get("repo_published") is not True:
                unpublished = True
                break
    if unpublished:
        return 0
    names = sorted({_job_pkg_name(j) for j in closed if _job_pkg_name(j)})
    note = "deadlock: wait cycle " + ", ".join(names)
    n = 0
    for job in closed:
        job.status = BuildStatus.FAILED
        job.error_message = note[:8000]
        job.blocked_reason = note
        n += 1
    await db.flush()
    return n


async def evaluate_layer_buildability(
    db: Any, layer_plan_id: int, source_name: str | None = None,
    published_architecture: str | None = None,
) -> list[BuildJob]:
    plan = await db.get(LayerPlan, int(layer_plan_id))
    if plan is None or getattr(plan, "status", None) not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
        return []
    release = await db.get(Release, plan.release_id)
    tag = await db.get(SideTag, plan.side_tag_id) if plan.side_tag_id else None
    if release is None or tag is None:
        return []
    jobs = list((await db.execute(
        select(BuildJob)
        .options(selectinload(BuildJob.package))
        .where(
            BuildJob.layer_plan_id == plan.id,
            BuildJob.status == BuildStatus.BLOCKED,
        )
    )).scalars().all())
    if published_architecture:
        want_arch = normalize_machine_arch(published_architecture)
        jobs = [j for j in jobs if _job_arch(j) == want_arch]
    changed: list[BuildJob] = []
    for job in jobs:
        before = (job.status, tuple(job.blocking_sources or ()), job.blocked_reason)
        await probe_job_target(job, release.releasename, tag.name)
        if job.status == BuildStatus.QUEUED:
            job.blocking_sources = []
            await _replace_blockers(db, job, [])
        if before != (job.status, tuple(job.blocking_sources or ()), job.blocked_reason):
            changed.append(job)
    still_blocked = [j for j in jobs if j.status == BuildStatus.BLOCKED]
    if still_blocked:
        await expand_blocked_jobs(db, plan, release, tag, still_blocked)
    await db.flush()
    return changed


def schedule_layer_evaluation(plan_id: int) -> None:
    from oreon_build.db.session import async_session_maker
    from oreon_build.services.build_trigger_bg import spawn
    async def run() -> None:
        async with async_session_maker() as db:
            await evaluate_layer_buildability(db, int(plan_id))
            from oreon_build.services.layer_plan import maybe_settle_layer_plan
            await maybe_settle_layer_plan(db, int(plan_id))
            await db.commit()
    spawn(run())


async def start_phase2_purification(db: Any, plan_id: int) -> list[BuildJob]:
    return []


async def _running_plan_ids_waiting_on(db: Any, releasename: str, source_name: str) -> set[int]:
    q = (
        select(BuildJob.layer_plan_id)
        .join(LayerPlan, LayerPlan.id == BuildJob.layer_plan_id)
        .join(Release, Release.id == LayerPlan.release_id)
        .join(LayerJobBlocker, LayerJobBlocker.build_job_id == BuildJob.id)
        .where(
            Release.releasename == releasename,
            LayerPlan.status.in_((LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING)),
            LayerJobBlocker.blocking_source == source_name,
        )
        .distinct()
    )
    rows = await db.execute(q)
    return {int(pid) for (pid,) in rows.all() if pid is not None}


async def on_package_published(source_name: str, releasename: str, published_architecture: str | None = None, job_id: int | None = None) -> None:
    from oreon_build.db.session import async_session_maker
    from oreon_build.services.layer_plan import maybe_settle_layer_plan
    async with async_session_maker() as db:
        query = select(BuildJob).join(LayerPlan).join(Release).where(
            Release.releasename == releasename,
            LayerPlan.status.in_((LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING)),
        )
        if job_id is not None:
            query = query.where(BuildJob.id == int(job_id))
        published = list((await db.execute(query)).scalars().all())
        plan_ids = {
            int(j.layer_plan_id) for j in published
            if j.layer_plan_id is not None
        }
        for job in published:
            opts = job_options_dict(job)
            opts["repo_published"] = True
            job.job_options = opts
            schedule_next_pass_after_publish(job)
            job_arch = str(job_options_dict(job).get("architecture") or "x86_64")
            invalidate_repo_solver_cache(releasename, job.branch, [published_architecture or job_arch])
        if source_name:
            plan_ids |= await _running_plan_ids_waiting_on(db, releasename, source_name)
        await db.flush()
        for plan_id in plan_ids:
            await evaluate_layer_buildability(db, plan_id, source_name, published_architecture)
            await maybe_settle_layer_plan(db, plan_id)
        await db.commit()


async def reconcile_settled_layer_plans() -> None:
    from oreon_build.db.session import async_session_maker
    from oreon_build.services.layer_plan import maybe_settle_layer_plan
    async with async_session_maker() as db:
        plans = list((await db.execute(select(LayerPlan).where(
            LayerPlan.status == LayerPlanStatus.RUNNING))).scalars().all())
        for plan in plans:
            await evaluate_layer_buildability(db, plan.id)
            await maybe_settle_layer_plan(db, plan.id)
        await db.commit()


async def reconcile_stuck_layer_blockers() -> None:
    await reconcile_settled_layer_plans()


async def finalize_blocked_on_dead_upstreams(db: Any, plan_id: int) -> int: return 0
async def finalize_idle_blocked_jobs(db: Any, plan_id: int) -> int: return 0
async def resolve_layer_blocker_cycles(db: Any, plan_id: int, cap_index: Any = None) -> int:
    return await _fail_wait_cycles(db, plan_id)
async def cancel_layer_blocker_cycles(db: Any, plan_id: int) -> int: return 0
