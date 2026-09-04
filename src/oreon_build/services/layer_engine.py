"""Two-phase selectable-fallback bridge and native purification scheduler.

No dependency DAG, spec parser, shadow Provides overlay, or cycle breaker is
used here. Routing is based only on BuildRequires emitted by SRPM preparation
and native libsolv queries against published repodata.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Iterable

from fastapi import HTTPException, Request
from sqlalchemy import func, select

from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.dnf_missing import parse_dnf_missing_provides
from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.core.repo_branch import release_distgit_branch
from oreon_build.models import BuildJob, BuildStatus, LayerPlan, LayerPlanStatus, MockEnvironment, Release, SideTag
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
ACTIVE = (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.BLOCKED, BuildStatus.RUNNING)
EXECUTING = (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)
_LAYER_JOB_WRITABLE = frozenset((BuildStatus.QUEUED, BuildStatus.BLOCKED))


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
    native = str(job_options_dict(job).get("native_mock_config") or "")
    return bool(native) and str(job.target_mock_config or "") != native


def _arch_config(config_name: str, arch: str) -> str:
    base = str(config_name or "").strip()
    if not base:
        return ""
    for suffix in ("-x86_64", "-aarch64"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    return f"{base}-{arch}"


async def probe_job_target(job: BuildJob, releasename: str, side_tag_repo_path: str) -> str:
    """Route a Phase-1 job by querying real repository metadata with libsolv."""
    options = job_options_dict(job)
    arch = normalize_machine_arch(str(options.get("architecture") or "x86_64"))
    branch = os.path.basename(os.path.normpath(side_tag_repo_path)) or str(job.branch or "dev")
    requirements = _requirements(job)

    # Cold seeds do not have trustworthy BuildRequires yet. The worker's SRPM
    # preparation/builddep result will populate solver_requirements and invoke
    # the fallback transition; resolving an empty transaction would falsely
    # classify every package as native-buildable.
    if requirements is None:
        job.target_mock_config = str(options.get("native_mock_config") or f"oreon-11-{arch}")
        job.status = BuildStatus.QUEUED
        job.blocked_reason = None
        return job.target_mock_config

    def solve(release_name: str, branches: list[str]) -> Any:
        return get_repo_solver(
            release_name, branches[0], [arch], extra_branches=branches[1:]
        ).resolve(requirements)

    native = await asyncio.to_thread(solve, releasename, [branch])
    if native.ok:
        job.target_mock_config = str(options.get("native_mock_config") or f"oreon-11-{arch}")
        job.status = BuildStatus.QUEUED
        job.blocked_reason = None
        return job.target_mock_config

    missing = sorted(native.missing_provides or ())
    fallback_config = str(options.get("fallback_mock_config") or "").strip()
    if not fallback_config:
        job.status = BuildStatus.BLOCKED
        job.blocked_reason = "waiting on repository: " + ", ".join(missing)
        return str(options.get("native_mock_config") or f"oreon-11-{arch}")
    job.target_mock_config = fallback_config
    job.status = BuildStatus.QUEUED
    detail = ", ".join(missing) or native.problems or "native dependencies unavailable"
    job.blocked_reason = f"routed to selected fallback due to missing native deps: {detail}"[:8000]
    return job.target_mock_config


async def create_pending_layer_plan(
    db: Any, request: Request | None, user: "Account", *, root_packages: list[str],
    release_id: int, side_tag_id: int, architectures: list[str] | None,
    preferred_mock_environment_id: int | None, fallback_mock_environment_id: int | None = None,
    distgit_url: str | None = None,
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
    if fallback_mock_environment_id is not None:
        fallback = await db.get(MockEnvironment, fallback_mock_environment_id)
        if fallback is None or fallback.release_id != release_id:
            raise HTTPException(status_code=400, detail="Fallback mock environment does not belong to release")
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
            "fallback_mock_environment_id": fallback_mock_environment_id,
            "priority": int(priority or 0),
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
    fallback_id = opts.get("fallback_mock_environment_id")
    fallback_env = await db.get(MockEnvironment, int(fallback_id)) if fallback_id is not None else None
    normalized_arch = normalize_machine_arch(arch)
    native_config = _arch_config(native_env.config_name if native_env else "oreon-11", normalized_arch)
    fallback_config = _arch_config(fallback_env.config_name, normalized_arch) if fallback_env else ""
    job = BuildJob(
        package_id=package_id, package_version_id=version_id, release_id=release.id,
        target_id=target.id, side_tag_id=tag.id, layer_plan_id=plan.id,
        status=BuildStatus.QUEUED, build_phase=PHASE1_BRIDGE,
        target_mock_config=native_config,
        priority=int(opts.get("priority") or 0), triggered_by_id=plan.triggered_by_id,
        branch=tag.name, layer_index=0,
        job_options={
            "architecture": normalized_arch,
            "native_mock_config": native_config,
            "fallback_mock_config": fallback_config,
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
    await asyncio.gather(*(probe_job_target(job, releasename, tag.name) for job in jobs))
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


async def evaluate_layer_buildability(
    db: Any, layer_plan_id: int, source_name: str | None = None,
    published_architecture: str | None = None,
) -> list[BuildJob]:
    """Perform one non-recursive repodata pass over waiting Phase-1 jobs."""
    plan = await db.get(LayerPlan, int(layer_plan_id))
    if plan is None or plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
        return []
    release = await db.get(Release, plan.release_id)
    tag = await db.get(SideTag, plan.side_tag_id) if plan.side_tag_id else None
    if release is None or tag is None:
        return []
    jobs = list((await db.execute(select(BuildJob).where(
        BuildJob.layer_plan_id == plan.id, BuildJob.build_phase == PHASE1_BRIDGE,
        BuildJob.status.in_((BuildStatus.QUEUED, BuildStatus.BLOCKED)),
    ))).scalars().all())
    changed: list[BuildJob] = []
    for job in jobs:
        before = (job.status, job.target_mock_config)
        await probe_job_target(job, release.releasename, tag.name)
        if before != (job.status, job.target_mock_config):
            changed.append(job)
    await db.flush()
    return changed


def schedule_layer_evaluation(plan_id: int) -> None:
    from oreon_build.db.session import async_session_maker
    from oreon_build.services.build_trigger_bg import spawn
    async def run() -> None:
        async with async_session_maker() as db:
            await evaluate_layer_buildability(db, int(plan_id))
            await db.commit()
    spawn(run())


async def start_phase2_purification(db: Any, plan_id: int) -> list[BuildJob]:
    plan = await db.get(LayerPlan, int(plan_id))
    if plan is None or plan.status != LayerPlanStatus.RUNNING:
        return []
    phase1 = list((await db.execute(select(BuildJob).where(
        BuildJob.layer_plan_id == plan.id, BuildJob.build_phase == PHASE1_BRIDGE,
    ))).scalars().all())
    if not phase1:
        return []
    # Do not fail the plan while work already dispatched in Phase 1 is still
    # running. Preserve its results/publications before settling the phase.
    if any(j.status in EXECUTING for j in phase1):
        return []
    if any(j.status in (BuildStatus.FAILED, BuildStatus.CANCELLED) for j in phase1):
        plan.status = LayerPlanStatus.FAILED
        plan.error_message = "Bridge pass failed on one or more packages"
        await db.flush()
        return []
    if any(j.status == BuildStatus.BLOCKED for j in phase1):
        return []
    if any(
        job_options_dict(j).get("repo_published") is not True
        for j in phase1 if j.status == BuildStatus.SUCCESS
    ):
        return []
    bridge = [j for j in phase1 if _built_on_fallback(j)]
    if not bridge:
        plan.status, plan.error_message = LayerPlanStatus.SUCCESS, None
        await db.flush()
        return []
    if await db.scalar(select(BuildJob.id).where(
        BuildJob.layer_plan_id == plan.id, BuildJob.build_phase == PHASE2_NATIVE).limit(1)):
        return []
    created: list[BuildJob] = []
    for old in bridge:
        phase2_options = dict(old.job_options or {})
        phase2_options.pop("repo_published", None)
        phase2_options["purification_of_job_id"] = old.id
        job = BuildJob(
            package_id=old.package_id, package_version_id=old.package_version_id,
            release_id=old.release_id, target_id=old.target_id, side_tag_id=old.side_tag_id,
            layer_plan_id=old.layer_plan_id, status=BuildStatus.QUEUED,
            build_phase=PHASE2_NATIVE,
            target_mock_config=str(job_options_dict(old).get("native_mock_config") or old.target_mock_config),
            blocked_reason="rebuilding clean native pass", priority=old.priority,
            triggered_by_id=old.triggered_by_id, branch=old.branch, layer_index=old.layer_index,
            job_options=phase2_options,
        )
        db.add(job)
        created.append(job)
    await db.flush()
    return created


async def on_package_published(source_name: str, releasename: str, published_architecture: str | None = None, job_id: int | None = None) -> None:
    from oreon_build.db.session import async_session_maker
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
            if j.layer_plan_id is not None and j.build_phase in (PHASE1_BRIDGE, PHASE2_NATIVE)
        }
        for job in published:
            opts = job_options_dict(job)
            opts["repo_published"] = True
            job.job_options = opts
            schedule_next_pass_after_publish(job)
            job_arch = str(job_options_dict(job).get("architecture") or "x86_64")
            invalidate_repo_solver_cache(releasename, job.branch, [published_architecture or job_arch])
        # Persist the specific publication event before reloading the complete
        # plan in evaluate/start_phase2. Concurrent publisher callbacks can
        # then observe one another's repo_published transitions reliably.
        await db.flush()
        for plan_id in plan_ids:
            await evaluate_layer_buildability(db, plan_id, source_name, published_architecture)
            await start_phase2_purification(db, plan_id)
            p2_active = await db.scalar(select(func.count()).select_from(BuildJob).where(
                BuildJob.layer_plan_id == plan_id, BuildJob.build_phase == PHASE2_NATIVE,
                BuildJob.status.in_(ACTIVE)))
            p2_failed = await db.scalar(select(func.count()).select_from(BuildJob).where(
                BuildJob.layer_plan_id == plan_id, BuildJob.build_phase == PHASE2_NATIVE,
                BuildJob.status.in_((BuildStatus.FAILED, BuildStatus.CANCELLED))))
            p2_total = await db.scalar(select(func.count()).select_from(BuildJob).where(
                BuildJob.layer_plan_id == plan_id, BuildJob.build_phase == PHASE2_NATIVE))
            if p2_total and not p2_active:
                plan = await db.get(LayerPlan, plan_id)
                plan.status = LayerPlanStatus.FAILED if p2_failed else LayerPlanStatus.SUCCESS
                plan.error_message = "Native purification failed" if p2_failed else None
        await db.commit()


async def reconcile_settled_layer_plans() -> None:
    from oreon_build.db.session import async_session_maker
    async with async_session_maker() as db:
        plans = list((await db.execute(select(LayerPlan).where(
            LayerPlan.status == LayerPlanStatus.RUNNING))).scalars().all())
        for plan in plans:
            await start_phase2_purification(db, plan.id)
        await db.commit()


async def reconcile_stuck_layer_blockers() -> None:
    await reconcile_settled_layer_plans()


async def finalize_blocked_on_dead_upstreams(db: Any, plan_id: int) -> int: return 0
async def finalize_idle_blocked_jobs(db: Any, plan_id: int) -> int: return 0
async def resolve_layer_blocker_cycles(db: Any, plan_id: int, cap_index: Any = None) -> int: return 0
async def cancel_layer_blocker_cycles(db: Any, plan_id: int) -> int: return 0
