"""OBS-style reactive layer scheduler.

Layer jobs are seeded in a blocked state, resolved against repository
capabilities, and released as repository publish events make dependencies
available. Dependency cycles are handled with a persisted bootstrap pass and
returned to the full profile after publication.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status
from sqlalchemy import exists, select, update
from sqlalchemy.orm import selectinload

from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.dnf_missing import parse_dnf_missing_provides
from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.core.repo_branch import release_distgit_branch
from oreon_build.models import (
    BuildJob,
    BuildStatus,
    BuildTarget,
    LayerPlan,
    LayerPlanStatus,
    MockEnvironment,
    PublishTask,
    Package,
    Release,
    Worker,
)
from oreon_build.services.build_pass import (
    apply_plan_to_job_options,
    bootstrap_dropped_sources,
    job_options_dict,
    schedule_next_pass_after_publish,
)
from oreon_build.core.rpm_requires import (
    evr_satisfies,
    is_chain_plan_requirement,
    parse_versioned_requirement,
    requirement_check_capabilities,
    rpm_evr_compare,
    split_requirement_evr,
)
from oreon_build.services.build_target_util import get_or_create_build_target, resolve_mock_environment_id
from oreon_build.services.dep_plan.providers import (
    filter_providers_for_consumer,
    is_base_system_package,
    is_toolchainish_source,
    narrow_alternate_providers,
    should_seed_providers,
    providers_for_requirement,
)
from oreon_build.services.distgit_order import (
    _fetch_distgit_specs_batch,
    _github_owner_repo_from_git_url,
    _spec_parse_fn,
    build_dependency_capabilities,
    get_distgit_provides_index_cached,
)
from oreon_build.services.layer_plan import get_or_create_package
from oreon_build.services.repo_capability_index import (
    RepoCapabilityIndex,
    default_capability_extra_branches,
    load_repo_capability_index,
)
from oreon_build.services.repo_solver import invalidate_repo_solver_cache

if TYPE_CHECKING:
    from oreon_build.models import Account

logger = logging.getLogger(__name__)

DEFAULT_ARCH = ["x86_64", "aarch64"]

_REACTIVE_CACHE_TTL = 300.0
_REACTIVE_CACHE_LIMIT = 2048
_REACTIVE_REQUIREMENTS_CACHE_VERSION = "static-br-v7"
_REACTIVE_CACHE_LOCK = threading.RLock()
_LAYER_EVAL_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="layer-eval")
_REACTIVE_SPEC_CACHE: dict[tuple[str, str, str, str], tuple[float, str]] = {}
_REACTIVE_REQUIREMENTS_CACHE: dict[tuple[str, str, str, str, str], tuple[float, tuple[str, ...], bool]] = {}
_REACTIVE_INDEX_CACHE: dict[tuple[str, str, str], tuple[float, RepoCapabilityIndex]] = {}
_REACTIVE_SOLVER_CACHE: dict[tuple[str, str, str], tuple[float, Any]] = {}
_REACTIVE_PLAN_LOCKS: dict[int, asyncio.Lock] = {}
_REACTIVE_EXPANSION_MAX_DEPTH = 32


def _reactive_cache_get(cache: dict, key: tuple) -> Any | None:
    now = time.monotonic()
    with _REACTIVE_CACHE_LOCK:
        value = cache.get(key)
        if value is None or now - value[0] >= _REACTIVE_CACHE_TTL:
            cache.pop(key, None)
            return None
        return value[1]


def _reactive_cache_put(cache: dict, key: tuple, value: Any) -> None:
    with _REACTIVE_CACHE_LOCK:
        if len(cache) >= _REACTIVE_CACHE_LIMIT:
            oldest = min(cache, key=lambda item: cache[item][0])
            cache.pop(oldest, None)
        cache[key] = (time.monotonic(), value)


async def _layer_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    if kwargs:
        return await loop.run_in_executor(_LAYER_EVAL_POOL, lambda: func(*args, **kwargs))
    return await loop.run_in_executor(_LAYER_EVAL_POOL, func, *args)

def _invalidate_reactive_repo_cache(releasename: str, branch: str, arch: str) -> None:
    key = (releasename, branch, arch)
    with _REACTIVE_CACHE_LOCK:
        _REACTIVE_INDEX_CACHE.pop(key, None)
        _REACTIVE_SOLVER_CACHE.pop(key, None)


def parse_missing_provides_from_text(text: str) -> set[str]:
    return set(parse_dnf_missing_provides(text))


async def _capability_arches_for_job(
    db: Any, job: BuildJob, plan_arches: list[str]
) -> list[str]:
    """Prefer the failing job's arch for prefer-repo checks.

    Dual-arch capability indexes intersect provides. A crate on side-tag x86_64
    only then looks missing and autofix rebuilds the whole graph for nothing.

    Do not touch job.architecture. That property lazy-loads build_target and
    blows up with MissingGreenlet under async SQLAlchemy.
    """
    arch = None
    loaded = None
    try:
        loaded = job.__dict__.get("build_target")
    except Exception:
        loaded = None
    if loaded is not None:
        arch = getattr(loaded, "architecture", None)
    tid = getattr(job, "target_id", None)
    if not arch and tid is not None:
        target = await db.get(BuildTarget, tid)
        if target is not None:
            arch = target.architecture
    if arch:
        return [normalize_machine_arch(str(arch))]
    return [str(a) for a in plan_arches if a] or list(DEFAULT_ARCH)


def _plan_options(plan: LayerPlan) -> dict[str, Any]:
    raw = plan.plan_options
    return dict(raw) if isinstance(raw, dict) else {}


async def _create_job_for_package(
    db: Any,
    plan: LayerPlan,
    pkg_name: str,
    *,
    release: Release,
    architectures: list[str],
    distgit_url: str,
    distgit_branch: str,
    preferred_mock_environment_id: int | None,
    priority: int,
    triggered_by_id: int | None,
    request: Request | None,
    releasename: str,
    publish_branch: str,
    layer_index: int,
    initial_status: BuildStatus,
    blocked_reason: str | None,
) -> list[BuildJob]:
    package_id, package_version_id = await get_or_create_package(
        db,
        pkg_name,
        release_id=release.id,
        releasename=releasename,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        preferred_mock_environment_id=preferred_mock_environment_id,
        triggered_by_id=triggered_by_id,
        request=request,
        layer_plan_ids=[plan.id],
        skip_audit=True,
    )
    pref_mock = resolve_mock_environment_id(
        trigger_mock_environment_id=preferred_mock_environment_id,
        package_mock_environment_id=None,
        release_default_mock_environment_id=release.default_mock_environment_id,
    )
    jobs: list[BuildJob] = []
    for arch in architectures:
        canon = normalize_machine_arch(arch)
        target = await get_or_create_build_target(db, release.id, canon, pref_mock)
        job = BuildJob(
            package_id=package_id,
            release_id=release.id,
            target_id=target.id,
            package_version_id=package_version_id,
            status=initial_status,
            priority=priority,
            triggered_by_id=triggered_by_id,
            branch=publish_branch,
            side_tag_id=plan.side_tag_id,
            layer_plan_id=plan.id,
            layer_index=layer_index,
            blocked_reason=blocked_reason,
        )
        db.add(job)
        jobs.append(job)
    return jobs


def initial_job_status(blockers: set[str] | frozenset[str]) -> tuple[BuildStatus, str | None]:
    """Pure decision: no unbuilt deps -> queue now, otherwise block on them."""
    if not blockers:
        return BuildStatus.QUEUED, None
    ordered = sorted(blockers)
    return BuildStatus.BLOCKED, f"waiting on: {', '.join(ordered[:8])}"


def _github_headers() -> dict[str, str]:
    import os

    from oreon_build.config import get_settings

    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    token = (getattr(get_settings(), "github_token", None) or os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def create_pending_layer_plan(
    db: Any,
    request: Request | None,
    user: "Account",
    *,
    root_packages: list[str],
    release_id: int,
    side_tag_id: int,
    architectures: list[str] | None,
    preferred_mock_environment_id: int | None,
    distgit_url: str | None,
    distgit_branch: str | None,
    priority: int,
) -> LayerPlan:
    """Fast path: validate inputs and create a PENDING LayerPlan. Heavy seeding is background."""
    from oreon_build.services.side_tags import bump_side_tag_activity, resolve_required_side_tag

    roots = sorted({(r or "").strip() for r in root_packages if (r or "").strip()})
    if not roots:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No packages given")

    rr = await db.execute(select(Release).where(Release.id == release_id))
    release = rr.scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    side_tag = await resolve_required_side_tag(
        db, release_id=release_id, side_tag_id=side_tag_id, user=user, require_access=True
    )

    if preferred_mock_environment_id:
        me_r = await db.execute(select(MockEnvironment).where(MockEnvironment.id == preferred_mock_environment_id))
        me = me_r.scalar_one_or_none()
        if not me or me.release_id != release_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Mock environment not found or does not belong to the selected release.",
            )

    arches = [(a or "").strip() for a in (architectures or DEFAULT_ARCH) if (a or "").strip()] or DEFAULT_ARCH
    dg_url = (distgit_url or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    dg_branch = (distgit_branch or release_distgit_branch(release)).strip()
    gr = _github_owner_repo_from_git_url(dg_url)
    if not gr:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"distgit_url is not a supported github.com URL: {dg_url!r}",
        )

    plan = LayerPlan(
        release_id=release_id,
        triggered_by_id=user.id,
        status=LayerPlanStatus.PENDING,
        root_packages=roots,
        plan_options={
            "architectures": arches,
            "distgit_url": dg_url,
            "distgit_branch": dg_branch,
            "preferred_mock_environment_id": preferred_mock_environment_id,
            "priority": priority,
        },
        side_tag_id=side_tag.id,
    )
    db.add(plan)
    await db.flush()
    await bump_side_tag_activity(db, side_tag.id)
    await log_audit(
        db,
        "build.layer_trigger",
        account_id=user.id,
        resource_type="layer_plan",
        resource_id=str(plan.id),
        details=" ".join(roots)[:512],
        ip_address=get_client_ip(request) if request is not None else None,
        user_agent=get_user_agent(request) if request is not None else "layer-build",
    )
    return plan


def _manifest_has_dynamic_buildrequires(spec_text: str, parsed: Any) -> bool:
    if bool(getattr(parsed, "has_generate_buildrequires", False)):
        return True
    from oreon_build.services.dynamic_brs import (
        spec_has_generate_buildrequires,
        spec_uses_cargo_generate_buildrequires,
    )
    return spec_has_generate_buildrequires(spec_text) or spec_uses_cargo_generate_buildrequires(spec_text)


def _manifest_build_requirements(
    spec_text: str,
    parsed: Any,
    *,
    architecture: str,
    defines: dict[str, str] | None = None,
    releasename: str = "",
) -> set[str]:
    from oreon_build.core.spec_bcond import (
        active_build_brs,
        default_build_defines,
        default_distro_macros,
        parse_spec_bconds,
    )

    bcond = parse_spec_bconds(
        spec_text,
        distro_macros=default_distro_macros(releasename),
        target_arches={architecture},
    )
    merged = {**default_build_defines(bcond), **(defines or {})}
    requirements = set(active_build_brs(bcond, merged))
    rpm_brs = getattr(parsed, "rpm_build_requires", None)
    if rpm_brs:
        requirements.update(str(item).strip() for item in rpm_brs if str(item).strip())
    else:
        requirements.update(build_dependency_capabilities(parsed))
    return {requirement for requirement in requirements if requirement and "%" not in requirement}


async def _unlock_manifest_leaf_jobs(
    db: Any,
    jobs: list[BuildJob],
    *,
    release: Release,
    side_tag_name: str,
    owner: str,
    repo: str,
    distgit_branch: str,
    releasename: str,
    headers: dict[str, str],
    provider_map: dict[str, set[str]],
    specs_by_package: dict[str, str],
) -> None:
    jobs_by_arch: dict[str, dict[str, BuildJob]] = {}
    packages: dict[int, Package] = {}
    for job in jobs:
        package = packages.get(int(job.package_id))
        if package is None:
            package = await db.get(Package, job.package_id)
            if package is None:
                continue
            packages[int(job.package_id)] = package
        target = await db.get(BuildTarget, job.target_id)
        arch = normalize_machine_arch(str(target.architecture if target else ""))
        if arch:
            jobs_by_arch.setdefault(arch, {})[package.name.strip().lower()] = job

    for arch, package_jobs in jobs_by_arch.items():
        repo_index = await asyncio.to_thread(
            load_repo_capability_index,
            releasename,
            side_tag_name,
            [arch],
            extra_branches=default_capability_extra_branches(side_tag_name),
        )
        known_sources = set(package_jobs)
        parse_gate = asyncio.Semaphore(16)

        async def parse_package(package_name: str, text: str) -> tuple[str, Any | None]:
            async with parse_gate:
                try:
                    parsed = await asyncio.to_thread(
                        _spec_parse_fn(releasename, target_arches={arch}),
                        text,
                        f"{package_name}.spec",
                    )
                except Exception:
                    logger.exception(
                        "manifest spec parse failed package=%s arch=%s",
                        package_name,
                        arch,
                    )
                    return package_name, None
                return package_name, parsed

        parsed_specs = dict(await asyncio.gather(*(
            parse_package(package_name, text)
            for package_name, text in specs_by_package.items()
            if package_name in package_jobs and text
        )))
        for package_name, job in package_jobs.items():
            text = specs_by_package.get(package_name)
            if not text:
                job.blocked_reason = "waiting on repository: source unavailable"
                continue
            parsed = parsed_specs.get(package_name)
            if parsed is None:
                job.blocked_reason = "waiting on repository: spec parse failed"
                continue
            requirements = _manifest_build_requirements(
                text, parsed, architecture=arch, releasename=releasename
            )
            if _manifest_has_dynamic_buildrequires(text, parsed) and getattr(parsed, "dyn_br_complete", None) is not True:
                job.blocked_reason = "waiting on repository: dynamic buildrequires"
                continue
            dependencies: set[str] = set()
            unresolved: set[str] = set()
            for requirement in sorted(requirements):
                sources = _layer_sources_for_requirement(
                    requirement,
                    provider_map,
                    known_sources,
                )
                layer_sources = {
                    str(source).strip().lower()
                    for source in sources
                    if str(source).strip()
                } & set(package_jobs)
                layer_sources.discard(package_name)
                if layer_sources:
                    dependencies.update(layer_sources)
                if not repo_index.satisfies_requirement(requirement) and not layer_sources:
                    unresolved.add(requirement)
                if not sources and not repo_index.satisfies_requirement(requirement):
                    unresolved.add(requirement)
            logger.info(
                "manifest leaf check package=%s arch=%s requirements=%s providers=%s dependencies=%s unresolved=%s",
                package_name,
                arch,
                sorted(requirements),
                {requirement: sorted(_layer_sources_for_requirement(requirement, provider_map)) for requirement in sorted(requirements)},
                sorted(dependencies),
                sorted(unresolved),
            )
            if dependencies or unresolved:
                details = [f"{source} (layer job)" for source in sorted(dependencies)]
                details.extend(sorted(unresolved))
                job.status = BuildStatus.BLOCKED
                job.blocking_sources = sorted(dependencies)
                job.blocked_reason = "waiting on repository: " + ", ".join(details[:8])
                continue
            job.status = BuildStatus.QUEUED
            job.blocked_reason = None
            job.blocking_sources = []


async def complete_seed_layer_plan(
    db: Any,
    plan_id: int,
    *,
    headers: dict[str, str] | None = None,
) -> list[BuildJob]:
    plan = await db.get(LayerPlan, plan_id)
    if plan is None or plan.status != LayerPlanStatus.PENDING:
        return []
    existing = await db.scalar(
        select(BuildJob.id).where(BuildJob.layer_plan_id == plan.id).limit(1)
    )
    if existing is not None:
        return []
    release = await db.get(Release, plan.release_id)
    if release is None:
        await fail_seed(db, plan, "Release not found")
        return []
    from oreon_build.models import SideTag
    side_tag = await db.get(SideTag, plan.side_tag_id) if plan.side_tag_id else None
    if side_tag is None:
        await fail_seed(db, plan, "Side tag not found")
        return []
    options = _plan_options(plan)
    arches = [str(a) for a in (options.get("architectures") or DEFAULT_ARCH) if a] or list(DEFAULT_ARCH)
    distgit_url = str(options.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    distgit_branch = str(options.get("distgit_branch") or release_distgit_branch(release)).strip()
    jobs: list[BuildJob] = []
    for package_name in [str(x).strip() for x in (plan.root_packages or []) if str(x).strip()]:
        jobs.extend(await _create_job_for_package(
            db, plan, package_name, release=release, architectures=arches,
            distgit_url=distgit_url, distgit_branch=distgit_branch,
            preferred_mock_environment_id=options.get("preferred_mock_environment_id"),
            priority=int(options.get("priority") or 0), triggered_by_id=plan.triggered_by_id,
            request=None, releasename=(release.releasename or "").strip(),
            publish_branch=side_tag.name, layer_index=0,
            initial_status=BuildStatus.BLOCKED, blocked_reason="waiting on repository",
        ))
    if not jobs:
        await fail_seed(db, plan, "No jobs created for selected architectures.")
        return []
    plan.status = LayerPlanStatus.RUNNING
    await db.flush()
    return jobs


async def seed_layer_plan(
    db: Any,
    request: Request,
    user: "Account",
    *,
    root_packages: list[str],
    release_id: int,
    side_tag_id: int,
    architectures: list[str] | None,
    preferred_mock_environment_id: int | None,
    distgit_url: str | None,
    distgit_branch: str | None,
    priority: int,
    headers: dict[str, str] | None = None,
) -> tuple[LayerPlan, list[BuildJob]]:
    plan = await create_pending_layer_plan(
        db,
        request,
        user,
        root_packages=root_packages,
        release_id=release_id,
        side_tag_id=side_tag_id,
        architectures=architectures,
        preferred_mock_environment_id=preferred_mock_environment_id,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        priority=priority,
    )
    jobs = await complete_seed_layer_plan(db, plan.id, headers=headers)
    await db.refresh(plan)
    if plan.status == LayerPlanStatus.FAILED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=plan.error_message)
    asyncio.create_task(_run_background_evaluation(int(plan.id)))
    return plan, jobs


async def _run_background_evaluation(plan_id: int) -> None:
    from oreon_build.db.session import async_session_maker

    try:
        async with async_session_maker() as eval_db:
            changed = await evaluate_layer_buildability(eval_db, int(plan_id))
            await eval_db.commit()
            logger.info(
                "background direct layer evaluation complete layer_plan_id=%s queued=%s",
                plan_id,
                len(changed),
            )
    except Exception:
        logger.exception("background direct layer evaluation failed layer_plan_id=%s", plan_id)


async def fail_seed(db: Any, plan: LayerPlan, message: str) -> None:
    from oreon_build.services.layer_plan import fail_layer_plan

    await fail_layer_plan(db, plan, message, terminal=LayerPlanStatus.FAILED)
    await db.flush()


def _spec_cache_key(owner: str, repo: str, distgit_branch: str, releasename: str, pkg_name: str) -> tuple[str, str, str, str]:
    return (owner + "/" + repo, distgit_branch, releasename, pkg_name)


def _layer_job_needs_spec_prepare(
    blocking_sources: list[str] | None,
    architecture: str,
    layer_statuses: dict[tuple[str, str], set[BuildStatus]],
    layer_repo_ready: dict[tuple[str, str], bool] | None = None,
) -> bool:
    arch = normalize_machine_arch(str(architecture or ""))
    stored = [
        _canonical_layer_name(value)
        for value in (blocking_sources or [])
        if str(value).strip()
    ]
    if not stored:
        return True
    ready = layer_repo_ready or {}
    for name in stored:
        statuses = layer_statuses.get((name, arch), set())
        if not statuses:
            continue
        if not all(status in (BuildStatus.SUCCESS, BuildStatus.SKIPPED) for status in statuses):
            return False
        if not ready.get((name, arch), True):
            return False
    return True


async def _load_eval_spec_texts(
    names: list[str],
    *,
    owner: str,
    repo: str,
    distgit_branch: str,
    releasename: str,
    headers: dict[str, str],
) -> dict[str, str]:
    specs: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        cached = _reactive_cache_get(
            _REACTIVE_SPEC_CACHE, _spec_cache_key(owner, repo, distgit_branch, releasename, name)
        )
        if cached is not None:
            specs[name] = cached
        else:
            missing.append(name)
    if not missing:
        return specs
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), headers=headers) as client:
        batch = await _fetch_distgit_specs_batch(
            client,
            owner=owner,
            repo=repo,
            distgit_branch=distgit_branch,
            releasename=releasename,
            packages=missing,
        )
    for name, text in batch.items():
        if text:
            specs[name] = text
            _reactive_cache_put(
                _REACTIVE_SPEC_CACHE,
                _spec_cache_key(owner, repo, distgit_branch, releasename, name),
                text,
            )
    return specs


async def _refetch_spec_text(
    pkg_name: str, *, owner: str, repo: str, distgit_branch: str, releasename: str, headers: dict[str, str]
) -> str | None:
    batch = await _load_eval_spec_texts(
        [pkg_name],
        owner=owner,
        repo=repo,
        distgit_branch=distgit_branch,
        releasename=releasename,
        headers=headers,
    )
    return batch.get(pkg_name)


def _rich_dep_leaf_requirements(requirement: str) -> list[str]:
    """Leaf reqs from a rich boolean dep, e.g. (crate(x) >= 1 with crate(x) < 2~)."""
    text = (requirement or "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if not text:
        return []
    parts = re.split(r"\b(?:with|without|if|unless|and|or)\b", text, flags=re.IGNORECASE)
    leaves: list[str] = []
    for part in parts:
        token = part.strip().strip("{}").strip()
        if not token:
            continue
        name, op, ver = parse_versioned_requirement(token)
        if name and op and ver:
            leaves.append(f"{name} {op} {ver}")
        elif name:
            leaves.append(name)
    return leaves


async def _candidate_satisfies_requirements(
    candidate: str,
    requirements: list[str],
    *,
    owner: str,
    repo: str,
    distgit_branch: str,
    releasename: str,
    headers: dict[str, str],
) -> bool:
    """Check the candidate's spec EVRs against every versioned leaf bound."""
    text = await _refetch_spec_text(
        candidate, owner=owner, repo=repo, distgit_branch=distgit_branch,
        releasename=releasename, headers=headers,
    )
    if not text:
        return False
    parsed = await _layer_to_thread(
        _spec_parse_fn(releasename, target_arches=set(DEFAULT_ARCH)), text, f"{candidate}.spec"
    )
    epoch = str(getattr(parsed, "epoch", 0) or 0)
    version = str(getattr(parsed, "version", "") or "")
    release = str(getattr(parsed, "release", "") or "")
    if not version:
        return False
    cand_evr = split_requirement_evr(f"{epoch}:{version}-{release}" if release else f"{epoch}:{version}")
    for capability in requirements:
        for leaf in _rich_dep_leaf_requirements(capability) or ([capability] if capability else []):
            name, op, ver = parse_versioned_requirement(leaf)
            if not name or not op or not ver:
                continue
            if "(" in name:
                continue
            if evr_satisfies(op, cand_evr, split_requirement_evr(ver)):
                continue
            return False
    return True


async def _pick_expansion_candidates(
    missing: set[str],
    providers: dict[str, set[str]],
    *,
    owner: str,
    repo: str,
    distgit_branch: str,
    releasename: str,
    headers: dict[str, str],
    package_names: set[str] | None = None,
) -> list[tuple[str, int]]:
    """Best provider per missing capability, verified against version bounds.

    Candidates violating any explicit bound are dropped. Survivors ranked by
    how many missing capabilities they cover.
    """
    names = {_canonical_layer_name(str(name)) for name in (package_names or set()) if str(name).strip()}
    orig_by_lower = {
        _canonical_layer_name(str(name)): str(name).strip()
        for name in (package_names or set())
        if str(name).strip()
    }
    per_candidate_reqs: dict[str, list[str]] = {}
    for capability in sorted(missing):
        sources = _layer_sources_for_requirement(str(capability), providers, names)
        for source in sources:
            real = orig_by_lower.get(_canonical_layer_name(source))
            if not real:
                continue
            reqs = per_candidate_reqs.setdefault(real, [])
            if capability not in reqs:
                reqs.append(capability)
    return sorted(
        ((source, len(requirements)) for source, requirements in per_candidate_reqs.items()),
        key=lambda item: (-item[1], item[0]),
    )


async def _expand_missing_provider(
    db: Any,
    plan_id: int,
    missing: set[str],
    *,
    plan: LayerPlan,
    release: Release,
    side_tag_name: str,
    architectures: list[str],
    options: dict[str, Any],
    distgit_url: str,
    distgit_branch: str,
    owner: str,
    repo: str,
    releasename: str,
    provider_index: Any,
) -> list[BuildJob]:
    providers = provider_index.indexed_providers() if provider_index is not None else {}
    verified = await _pick_expansion_candidates(
        set(missing), providers, owner=owner, repo=repo,
        distgit_branch=distgit_branch, releasename=releasename, headers=_github_headers(),
        package_names=set(getattr(provider_index, "package_names", ()) or ()),
    )
    if not verified:
        return []
    locked = await db.execute(
        select(LayerPlan).where(LayerPlan.id == int(plan_id))
    )
    locked_plan = locked.scalar_one_or_none()
    if locked_plan is None or locked_plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
        return []
    current = dict(locked_plan.plan_options or {})
    visited = {str(value) for value in (current.get("expanded_sources") or []) if str(value).strip()}
    depth = int(current.get("expansion_depth") or 0)
    existing_rows = await db.execute(
        select(Package.name)
        .join(BuildJob, BuildJob.package_id == Package.id)
        .where(BuildJob.layer_plan_id == int(plan_id))
    )
    existing = {str(name) for (name,) in existing_rows.all() if name}
    selected = [
        candidate
        for candidate, _score in verified
        if candidate not in existing and candidate not in visited
    ]
    if not selected:
        return []
    depth += len(selected)
    visited.update(selected)
    current["expanded_sources"] = sorted(visited)
    current["expansion_depth"] = depth
    locked_plan.plan_options = current
    locked_plan.root_packages = list(locked_plan.root_packages or []) + selected
    new_jobs: list[BuildJob] = []
    for candidate in selected:
        new_jobs.extend(await _create_job_for_package(
            db, locked_plan, candidate, release=release, architectures=architectures,
            distgit_url=distgit_url, distgit_branch=distgit_branch,
            preferred_mock_environment_id=options.get("preferred_mock_environment_id"),
            priority=int(options.get("priority") or 0), triggered_by_id=locked_plan.triggered_by_id,
            request=None, releasename=releasename, publish_branch=side_tag_name, layer_index=0,
            initial_status=BuildStatus.BLOCKED, blocked_reason="waiting on repository",
        ))
    await db.flush()
    return new_jobs


_LAYER_JOB_WRITABLE = (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.BLOCKED)


async def _update_layer_job_if_active(
    db: Any,
    job_id: int,
    plan_id: int,
    *,
    status: BuildStatus | None = None,
    blocked_reason: str | None = None,
    blocking_sources: list[str] | None = None,
    error_message: str | None = None,
    completed_at: Any | None = None,
    clear_completed_at: bool = False,
    job_options: dict[str, Any] | None = None,
) -> bool:
    values: dict[str, Any] = {}
    if status is not None:
        values["status"] = status
    if blocked_reason is not None:
        values["blocked_reason"] = blocked_reason[:500]
    if blocking_sources is not None:
        values["blocking_sources"] = sorted({_canonical_layer_name(source) for source in blocking_sources if str(source).strip()})
    if error_message is not None:
        values["error_message"] = error_message[:8000]
    if completed_at is not None or clear_completed_at:
        values["completed_at"] = completed_at
    if job_options is not None:
        values["job_options"] = dict(job_options)
    if not values:
        return False
    result = await db.execute(
        update(BuildJob)
        .where(
            BuildJob.id == int(job_id),
            BuildJob.layer_plan_id == int(plan_id),
            BuildJob.status.in_(_LAYER_JOB_WRITABLE),
            exists(
                select(1).where(
                    LayerPlan.id == int(plan_id),
                    LayerPlan.status.in_((LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING)),
                )
            ),
        )
        .values(**values)
    )
    return bool(result.rowcount)


def _canonical_layer_name(value: str) -> str:
    name = (value or "").strip().lower()
    if name.endswith(".spec"):
        name = name[:-5]
    return name


def _layer_sources_for_requirement(
    requirement: str,
    provider_map: dict[str, set[str]],
    package_names: set[str] | None = None,
) -> set[str]:
    names = {
        _canonical_layer_name(str(name))
        for name in (package_names or set())
        if str(name).strip()
    }
    sources: set[str] = set()
    restrict = bool(names)
    for capability in requirement_check_capabilities(requirement):
        cap = str(capability).strip()
        if not cap:
            continue
        primary = parse_versioned_requirement(cap)[0] or cap
        for key in (cap, cap.lower(), primary, primary.lower()):
            values = provider_map.get(key, set())
            if isinstance(values, (set, frozenset, list, tuple)):
                sources.update(
                    _canonical_layer_name(str(value))
                    for value in values
                    if str(value).strip()
                    and (not restrict or _canonical_layer_name(str(value)) in names)
                )
        canon = _canonical_layer_name(primary)
        if canon and (not restrict or canon in names):
            sources.add(canon)
    sources.update(
        _canonical_layer_name(source)
        for source in providers_for_requirement(
            requirement,
            provider_map=provider_map,
            package_names=names,
        )
        if _canonical_layer_name(source)
        and (not restrict or _canonical_layer_name(source) in names)
    )
    return sources


def _job_repo_published(status: BuildStatus, job_options: dict[str, Any] | None) -> bool:
    if status == BuildStatus.SKIPPED:
        return True
    if status != BuildStatus.SUCCESS:
        return False
    if not isinstance(job_options, dict) or "repo_published" not in job_options:
        return True
    return bool(job_options.get("repo_published"))


def _incomplete_layer_sources(
    requirements: tuple[str, ...] | set[str] | list[str],
    provider_map: dict[str, set[str]],
    package_names: set[str],
    layer_statuses: dict[tuple[str, str], set[BuildStatus]],
    architecture: str,
    layer_repo_ready: dict[tuple[str, str], bool] | None = None,
) -> set[str]:
    arch = normalize_machine_arch(str(architecture or ""))
    ready = layer_repo_ready or {}
    out: set[str] = set()
    for requirement in requirements:
        for source in _layer_sources_for_requirement(str(requirement), provider_map, package_names):
            name = _canonical_layer_name(source)
            statuses = layer_statuses.get((name, arch), set())
            if not statuses:
                continue
            if not all(status in (BuildStatus.SUCCESS, BuildStatus.SKIPPED) for status in statuses):
                out.add(name)
                continue
            if not ready.get((name, arch), True):
                out.add(name)
    return out


def _missing_caps_after_bootstrap_drops(
    missing: list[str] | set[str] | tuple[str, ...],
    dropped_sources: set[str],
    provider_map: dict[str, set[str]],
    package_names: set[str],
) -> list[str]:
    caps = [str(cap) for cap in missing if str(cap).strip()]
    if not dropped_sources or not caps:
        return caps
    kept: list[str] = []
    for cap in caps:
        sources = {
            _canonical_layer_name(source)
            for source in _layer_sources_for_requirement(cap, provider_map, package_names)
        }
        if sources and sources <= dropped_sources:
            continue
        kept.append(cap)
    return kept


def _requirement_matches_sources(
    requirement: str,
    sources: set[str],
    provider_map: dict[str, set[str]],
    package_names: set[str],
) -> bool:
    if not sources:
        return False
    normalized_sources = {_canonical_layer_name(source) for source in sources}
    resolved = _layer_sources_for_requirement(requirement, provider_map, package_names)
    if normalized_sources & {_canonical_layer_name(source) for source in resolved}:
        return True
    try:
        resolved = providers_for_requirement(
            requirement,
            provider_map=provider_map,
            package_names={_canonical_layer_name(name) for name in package_names},
        )
    except Exception:
        return False
    return bool(
        normalized_sources
        & {_canonical_layer_name(source) for source in resolved}
    )


def _solver_missing_caps(result: Any) -> set[str]:
    if bool(getattr(result, "ok", False)):
        return set()
    missing = {
        str(value).strip()
        for value in (getattr(result, "missing_provides", None) or ())
        if str(value).strip()
    }
    problems = str(getattr(result, "problems", "") or "")
    if problems:
        for match in re.finditer(
            r"nothing provides\s+(?P<cap>[^\n]+?)(?:\s+needed by|\s*$)",
            problems,
            re.IGNORECASE | re.MULTILINE,
        ):
            cap = (match.group("cap") or "").strip().rstrip(".")
            if cap and not cap.startswith("("):
                missing.add(cap)
    if not missing and problems:
        missing.add("solver conflict")
    return missing


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[set[str]]:
    reverse: dict[str, set[str]] = {node: set() for node in graph}
    for node, children in graph.items():
        reverse.setdefault(node, set())
        for child in children:
            reverse.setdefault(child, set()).add(node)
    visited: set[str] = set()
    order: list[str] = []
    for root in graph:
        if root in visited:
            continue
        visited.add(root)
        stack: list[tuple[str, bool]] = [(root, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                order.append(node)
                continue
            stack.append((node, True))
            for child in graph.get(node, ()):
                if child not in visited:
                    visited.add(child)
                    stack.append((child, False))
    components: list[set[str]] = []
    visited.clear()
    for root in reversed(order):
        if root in visited:
            continue
        component: set[str] = set()
        visited.add(root)
        stack = [root]
        while stack:
            node = stack.pop()
            component.add(node)
            for child in reverse.get(node, ()):
                if child not in visited:
                    visited.add(child)
                    stack.append(child)
        if len(component) > 1 or root in graph.get(root, set()):
            components.append(component)
    return components


def _bootstrap_drops_for_graph(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    drops: dict[str, set[str]] = {}
    for component in _strongly_connected_components(graph):
        for consumer in component:
            internal = graph.get(consumer, set()) & component
            if internal:
                drops[consumer] = set(internal)
    return drops


def _caps_provided_by_sources(
    sources: set[str],
    provider_map: dict[str, set[str]] | None,
) -> list[str]:
    want = {_canonical_layer_name(source) for source in sources if str(source).strip()}
    if not want or not provider_map:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for cap, pkgs in provider_map.items():
        token = str(cap).strip()
        if not token or token in seen:
            continue
        for pkg in pkgs:
            if _canonical_layer_name(str(pkg)) in want:
                seen.add(token)
                out.append(token)
                break
    return out


async def _start_bootstrap_pass(
    db: Any,
    job: BuildJob,
    dropped_sources: set[str],
    *,
    provider_map: dict[str, set[str]] | None = None,
    extra_stubs: set[str] | None = None,
) -> bool:
    dropped = sorted({_canonical_layer_name(source) for source in dropped_sources if str(source).strip()})
    if not dropped:
        return False
    opts = job_options_dict(job)
    if str(opts.get("build_profile") or "full").strip().lower() == "full" and opts.get("bootstrap_dropped"):
        return False
    stubs = {str(value).strip() for value in (extra_stubs or set()) if str(value).strip()}
    stubs.update(_caps_provided_by_sources(set(dropped), provider_map))
    opts["build_profile"] = "bootstrap"
    opts["rpmbuild_defines"] = {"with_check": "0"}
    opts["next_profile"] = "full"
    opts["bootstrap_dropped"] = dropped
    opts["bootstrap_stub_provides"] = sorted(stubs)[:800]
    await _update_layer_job_if_active(
        db,
        job.id,
        job.layer_plan_id,
        clear_completed_at=True,
        job_options=opts,
    )
    job.job_options = opts
    job.completed_at = None
    return True


async def _evaluate_layer_buildability(
    db: Any,
    layer_plan_id: int,
    source_name: str | None = None,
    published_architecture: str | None = None,
) -> list[BuildJob]:
    plan = await db.get(LayerPlan, int(layer_plan_id))
    if plan is None:
        return []
    stall = (plan.error_message or "").strip().lower()
    if plan.status == LayerPlanStatus.FAILED and stall.startswith("layer stalled with no queued or running jobs"):
        plan.status = LayerPlanStatus.RUNNING
        plan.error_message = None
        await db.flush()
    if plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
        return []
    release = await db.get(Release, plan.release_id)
    if release is None:
        return []
    options = _plan_options(plan)
    releasename = (release.releasename or "").strip()
    distgit_url = str(options.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    owner_repo = _github_owner_repo_from_git_url(distgit_url)
    if not owner_repo:
        return []
    owner, repo = owner_repo
    distgit_branch = str(options.get("distgit_branch") or release_distgit_branch(release)).strip()
    rows_result = await db.execute(
        select(BuildJob, BuildTarget.architecture)
        .join(BuildTarget, BuildTarget.id == BuildJob.target_id)
        .where(
            BuildJob.layer_plan_id == plan.id,
            BuildJob.status.in_((BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.BLOCKED)),
        )
    )
    row_list = list(rows_result.all())
    changed: list[BuildJob] = []
    provider_index = await get_distgit_provides_index_cached(
        owner=owner, repo=repo, distgit_branch=distgit_branch,
        releasename=releasename, headers=_github_headers(),
    )
    provider_map = provider_index.indexed_providers() if provider_index is not None else {}
    layer_rows_result = await db.execute(
        select(Package.name, BuildJob.status, BuildTarget.architecture, BuildJob.job_options)
        .join(BuildJob, BuildJob.package_id == Package.id)
        .join(BuildTarget, BuildTarget.id == BuildJob.target_id)
        .where(BuildJob.layer_plan_id == plan.id)
    )
    layer_rows = list(layer_rows_result.all())
    layer_statuses: dict[tuple[str, str], set[BuildStatus]] = {}
    layer_package_names: set[str] = set()
    layer_repo_ready: dict[tuple[str, str], bool] = {}
    for name, job_status, job_arch, job_opts in layer_rows:
        if name:
            canonical_name = _canonical_layer_name(str(name))
            layer_package_names.add(canonical_name)
            arch_key = normalize_machine_arch(str(job_arch or ""))
            layer_statuses.setdefault((canonical_name, arch_key), set()).add(job_status)
            published = _job_repo_published(
                job_status, job_opts if isinstance(job_opts, dict) else None
            )
            prev = layer_repo_ready.get((canonical_name, arch_key))
            layer_repo_ready[(canonical_name, arch_key)] = published if prev is None else (prev and published)
    published_arch = str(published_architecture or "").strip()
    arch_filter = normalize_machine_arch(published_arch) if published_arch else ""
    source_filter = _canonical_layer_name(source_name or "")
    if source_filter or arch_filter:
        row_list = [
            (job, architecture)
            for job, architecture in row_list
            if (
                not source_filter
                or not job.blocking_sources
                or source_filter in {
                    _canonical_layer_name(value)
                    for value in (job.blocking_sources or [])
                }
            )
            and (not arch_filter or normalize_machine_arch(str(architecture or "")) == arch_filter)
        ]
    all_pkg_ids = {int(job.package_id) for job, _architecture in row_list}
    packages_by_id: dict[int, Package] = {}
    if all_pkg_ids:
        pkg_rows = await db.execute(select(Package).where(Package.id.in_(all_pkg_ids)))
        packages_by_id = {int(pkg.id): pkg for pkg in pkg_rows.scalars().all()}
    mock_graph: dict[str, set[str]] = {}
    job_consumer: dict[int, str] = {}
    for job, _architecture in row_list:
        pkg = packages_by_id.get(int(job.package_id))
        if pkg is None or not pkg.name:
            continue
        consumer = _canonical_layer_name(pkg.name)
        job_consumer[int(job.id)] = consumer
        mock_graph.setdefault(consumer, set())
        for value in (job.blocking_sources or []):
            blocker = _canonical_layer_name(value)
            if blocker and blocker != consumer:
                mock_graph[consumer].add(blocker)
    mock_drops = _bootstrap_drops_for_graph(mock_graph)
    bootstrap_from_mock: dict[tuple[int, str], set[str]] = {}
    for job, architecture in row_list:
        consumer = job_consumer.get(int(job.id))
        if not consumer:
            continue
        drops = mock_drops.get(consumer)
        if not drops:
            continue
        arch = normalize_machine_arch(str(architecture or ""))
        bootstrap_from_mock[(int(job.id), arch)] = set(drops)
        await _start_bootstrap_pass(db, job, drops, provider_map=provider_map)
    work_list = [
        (job, architecture)
        for job, architecture in row_list
        if _layer_job_needs_spec_prepare(
            job.blocking_sources, str(architecture or ""), layer_statuses, layer_repo_ready
        )
        or (int(job.id), normalize_machine_arch(str(architecture or ""))) in bootstrap_from_mock
    ]
    logger.info(
        "layer eval start plan=%s open_jobs=%s prepare=%s",
        plan.id,
        len(row_list),
        len(work_list),
    )
    unique_names = sorted({
        packages_by_id[int(job.package_id)].name
        for job, _architecture in work_list
        if int(job.package_id) in packages_by_id and packages_by_id[int(job.package_id)].name
    })
    spec_by_name = await _load_eval_spec_texts(
        unique_names,
        owner=owner,
        repo=repo,
        distgit_branch=distgit_branch,
        releasename=releasename,
        headers=_github_headers(),
    )
    prepared: dict[tuple[int, str], tuple[Package, str | None, Any | None, tuple[str, ...], bool]] = {}
    prepare_gate = asyncio.Semaphore(16)

    async def prepare_job(job: BuildJob, architecture: str) -> None:
        arch = normalize_machine_arch(str(architecture or ""))
        package = packages_by_id.get(int(job.package_id))
        if not arch or package is None:
            return
        async with prepare_gate:
            text = spec_by_name.get(package.name)
            if not text:
                prepared[(int(job.id), arch)] = (package, None, None, (), False)
                return
            spec_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            opts_probe = job_options_dict(job)
            profile_probe = str(opts_probe.get("build_profile") or "full").strip().lower()
            profile_is_bootstrap = profile_probe == "bootstrap"
            req_key = (
                _REACTIVE_REQUIREMENTS_CACHE_VERSION,
                owner + "/" + repo,
                distgit_branch,
                package.name,
                arch,
                "bootstrap" if profile_is_bootstrap else "full",
                spec_digest,
            )
            parsed = await _layer_to_thread(
                _spec_parse_fn(releasename, target_arches={arch}), text, f"{package.name}.spec"
            )
            cached_req = _reactive_cache_get(_REACTIVE_REQUIREMENTS_CACHE, req_key)
            if cached_req is not None:
                requirements, complete = cached_req
            else:
                profile_defines = (
                    dict(opts_probe.get("rpmbuild_defines") or {}) if profile_is_bootstrap else {}
                )
                requirements = tuple(sorted(_manifest_build_requirements(
                    text, parsed, architecture=arch, defines=profile_defines or None,
                    releasename=releasename,
                )))
                complete = not _manifest_has_dynamic_buildrequires(text, parsed)
                _reactive_cache_put(_REACTIVE_REQUIREMENTS_CACHE, req_key, (requirements, complete))
            prepared[(int(job.id), arch)] = (package, text, parsed, tuple(requirements), complete)

    await asyncio.gather(*(prepare_job(job, architecture) for job, architecture in work_list))
    bootstrap_drops: dict[tuple[int, str], set[str]] = {}
    for architecture in {normalize_machine_arch(str(value or "")) for _, value in work_list}:
        arch_rows = [
            (job, prepared.get((int(job.id), normalize_machine_arch(str(arch or "")))) )
            for job, arch in work_list
            if normalize_machine_arch(str(arch or "")) == architecture
        ]
        graph: dict[str, set[str]] = {
            _canonical_layer_name(str(item[1][0].name)): set()
            for item in arch_rows
            if item[1] and item[1][0] is not None
        }
        for item in arch_rows:
            job, prepared_job = item
            if not prepared_job or prepared_job[0] is None:
                continue
            package, _, _, requirements, complete = prepared_job
            consumer = _canonical_layer_name(package.name)
            if not complete:
                continue
            for requirement in requirements:
                for source in _layer_sources_for_requirement(str(requirement), provider_map, layer_package_names):
                    provider = _canonical_layer_name(source)
                    if provider in graph and provider != consumer:
                        graph[consumer].add(provider)
        graph_drops = _bootstrap_drops_for_graph(graph)
        for job, prepared_job in arch_rows:
            if not prepared_job or prepared_job[0] is None:
                continue
            consumer = _canonical_layer_name(prepared_job[0].name)
            drops = graph_drops.get(consumer)
            if drops:
                bootstrap_drops[(int(job.id), architecture)] = drops
    for key, drops in bootstrap_from_mock.items():
        bootstrap_drops.setdefault(key, set()).update(drops)
    for job, architecture in work_list:
        if plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
            break
        arch = normalize_machine_arch(str(architecture or ""))
        prepared_job = prepared.get((int(job.id), arch))
        if prepared_job is None:
            continue
        package, text, parsed, requirements, complete = prepared_job
        dropped_sources = bootstrap_drops.get((int(job.id), arch), set())
        opts = job_options_dict(job)
        if dropped_sources and str(opts.get("build_profile") or "full").strip().lower() != "full":
            await _start_bootstrap_pass(db, job, dropped_sources, provider_map=provider_map)
            opts = job_options_dict(job)
        elif dropped_sources and not opts.get("bootstrap_dropped"):
            await _start_bootstrap_pass(db, job, dropped_sources, provider_map=provider_map)
            opts = job_options_dict(job)
        if str(opts.get("build_profile") or "full").strip().lower() == "bootstrap":
            dropped_sources = {
                _canonical_layer_name(value)
                for value in (opts.get("bootstrap_dropped") or dropped_sources)
                if str(value).strip()
            }
            if text and parsed is not None:
                profile_defines = dict(opts.get("rpmbuild_defines") or {"with_check": "0"})
                requirements = tuple(sorted(_manifest_build_requirements(
                    text, parsed, architecture=arch,
                    defines=profile_defines,
                    releasename=releasename,
                )))
                complete = True
        else:
            dropped_sources = set()
        effective_requirements = tuple(requirements)
        if dropped_sources:
            dropped_sources = {_canonical_layer_name(value) for value in dropped_sources if str(value).strip()}
        if not text:
            await _update_layer_job_if_active(
                db, job.id, plan.id, blocked_reason="waiting on repository: source unavailable"
            )
            continue
        layer_blockers = _incomplete_layer_sources(
            effective_requirements, provider_map, layer_package_names, layer_statuses, arch,
            layer_repo_ready,
        )
        if dropped_sources:
            layer_blockers -= dropped_sources
        failed_sources = sorted(
            source
            for source in layer_blockers
            if BuildStatus.FAILED in layer_statuses.get((source, arch), set())
            or BuildStatus.CANCELLED in layer_statuses.get((source, arch), set())
        )
        if failed_sources:
            await _update_layer_job_if_active(
                db,
                job.id,
                plan.id,
                status=BuildStatus.BLOCKED,
                blocked_reason="waiting on failed layer jobs: " + ", ".join(failed_sources[:8]),
                blocking_sources=sorted(layer_blockers),
            )
            continue
        if layer_blockers:
            await _update_layer_job_if_active(
                db,
                job.id,
                plan.id,
                status=BuildStatus.BLOCKED,
                blocked_reason="waiting on layer jobs: " + ", ".join(sorted(layer_blockers)[:8]),
                blocking_sources=sorted(layer_blockers),
            )
            continue
        if job.status != BuildStatus.QUEUED:
            changed_now = await _update_layer_job_if_active(
                db, job.id, plan.id, status=BuildStatus.QUEUED, blocked_reason="", blocking_sources=[]
            )
            if changed_now:
                changed.append(job)
        else:
            await _update_layer_job_if_active(db, job.id, plan.id, blocked_reason="", blocking_sources=[])
    await db.flush()
    logger.info(
        "layer eval done plan=%s queued=%s",
        plan.id,
        len(changed),
    )
    return changed



async def evaluate_layer_buildability(
    db: Any,
    layer_plan_id: int,
    source_name: str | None = None,
    published_architecture: str | None = None,
) -> list[BuildJob]:
    plan_id = int(layer_plan_id)
    lock = _REACTIVE_PLAN_LOCKS.get(plan_id)
    if lock is None:
        lock = asyncio.Lock()
        _REACTIVE_PLAN_LOCKS[plan_id] = lock
    async with lock:
        changed = await _evaluate_layer_buildability(
            db,
            plan_id,
            source_name=source_name,
            published_architecture=published_architecture,
        )
        from oreon_build.services.layer_plan import maybe_settle_layer_plan

        await maybe_settle_layer_plan(db, plan_id)
        return changed



async def _recheck_job_ready(
    db: Any,
    job: BuildJob,
    *,
    plan: LayerPlan,
    release: Release,
    cap_index: RepoCapabilityIndex | None = None,
    owner: str = "",
    repo: str = "",
    distgit_branch: str = "",
    releasename: str = "",
    headers: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
    prune: bool = True,
) -> None:
    await evaluate_layer_buildability(db, int(plan.id))


async def try_autofix_layer_missing_provides(
    db: Any,
    job: BuildJob,
    reason: str,
    *,
    package_name: str | None = None,
) -> bool:
    if job.layer_plan_id is None:
        return False
    missing = parse_missing_provides_from_text(reason)
    if not missing:
        return False
    plan = await db.get(LayerPlan, int(job.layer_plan_id))
    if plan is None or plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
        return False
    release = await db.get(Release, plan.release_id)
    if release is None:
        return False
    options = _plan_options(plan)
    releasename = (release.releasename or "").strip()
    distgit_url = str(options.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    owner_repo = _github_owner_repo_from_git_url(distgit_url)
    if not owner_repo:
        return False
    owner, repo = owner_repo
    distgit_branch = str(options.get("distgit_branch") or release_distgit_branch(release)).strip()
    from oreon_build.models import SideTag

    side_tag = await db.get(SideTag, plan.side_tag_id) if plan.side_tag_id else None
    side_tag_name = side_tag.name if side_tag is not None else str(options.get("branch") or "")
    architectures = [str(value) for value in (options.get("architectures") or DEFAULT_ARCH) if value] or list(DEFAULT_ARCH)
    provider_index = await get_distgit_provides_index_cached(
        owner=owner, repo=repo, distgit_branch=distgit_branch,
        releasename=releasename, headers=_github_headers(),
    )
    providers = provider_index.indexed_providers() if provider_index is not None else {}
    distgit_names = set(getattr(provider_index, "package_names", ()) or ())
    existing_rows = await db.execute(
        select(Package.name)
        .join(BuildJob, BuildJob.package_id == Package.id)
        .where(BuildJob.layer_plan_id == int(plan.id))
    )
    existing = {_canonical_layer_name(str(name)) for (name,) in existing_rows.all() if name}
    mapped: set[str] = set()
    for cap in missing:
        mapped.update(_layer_sources_for_requirement(str(cap), providers, distgit_names or existing))
    if provider_index is not None:
        added_jobs = await _expand_missing_provider(
            db, int(plan.id), set(missing), plan=plan, release=release,
            side_tag_name=side_tag_name, architectures=architectures, options=options,
            distgit_url=distgit_url, distgit_branch=distgit_branch, owner=owner, repo=repo,
            releasename=releasename, provider_index=provider_index,
        )
        for added in added_jobs:
            pkg = await db.get(Package, added.package_id)
            if pkg is not None and pkg.name:
                mapped.add(_canonical_layer_name(pkg.name))
    mapped = {name for name in mapped if name}
    if not mapped:
        return False
    self_name = _canonical_layer_name(package_name or "")
    mapped.discard(self_name)
    if not mapped:
        return False
    blocker_jobs = await db.execute(
        select(BuildJob, Package.name)
        .join(Package, Package.id == BuildJob.package_id)
        .where(BuildJob.layer_plan_id == int(plan.id))
    )
    cycle_sources: set[str] = set()
    for other, other_name in blocker_jobs.all():
        other_canon = _canonical_layer_name(str(other_name or ""))
        if other_canon not in mapped:
            continue
        blocked_on = {
            _canonical_layer_name(value)
            for value in (other.blocking_sources or [])
            if str(value).strip()
        }
        if self_name and self_name in blocked_on:
            cycle_sources.add(other_canon)
    job.status = BuildStatus.BLOCKED
    job.completed_at = None
    job.blocked_reason = "waiting on layer jobs: " + ", ".join(sorted(mapped)[:8])
    job.blocking_sources = sorted(mapped)
    await db.flush()
    if cycle_sources:
        cycle_caps = {
            cap
            for cap in missing
            if _layer_sources_for_requirement(str(cap), providers, distgit_names or existing) & cycle_sources
        }
        await _start_bootstrap_pass(
            db,
            job,
            cycle_sources,
            provider_map=providers,
            extra_stubs=cycle_caps,
        )
    await evaluate_layer_buildability(db, int(plan.id))
    return True


async def on_package_published(
    source_name: str, releasename: str, published_architecture: str | None = None
) -> None:
    from oreon_build.db.session import async_session_maker

    async with async_session_maker() as db:
        result = await db.execute(
            select(LayerPlan.id)
            .join(Release, Release.id == LayerPlan.release_id)
            .where(
                Release.releasename == releasename,
                LayerPlan.status.in_((
                    LayerPlanStatus.PENDING,
                    LayerPlanStatus.RUNNING,
                    LayerPlanStatus.FAILED,
                )),
            )
        )
        plan_ids = [int(plan_id) for (plan_id,) in result.all()]
        for plan_id in plan_ids:
            plan = await db.get(LayerPlan, plan_id)
            if plan is not None:
                transition = await db.execute(
                    select(BuildJob)
                    .join(Package, Package.id == BuildJob.package_id)
                    .join(BuildTarget, BuildTarget.id == BuildJob.target_id)
                    .where(
                        BuildJob.layer_plan_id == plan_id,
                        Package.name == source_name,
                        BuildJob.status.in_((BuildStatus.SUCCESS, BuildStatus.SKIPPED)),
                        BuildTarget.architecture == published_architecture
                        if published_architecture
                        else True,
                    )
                )
                for published_job in transition.scalars().all():
                    opts = job_options_dict(published_job)
                    opts["repo_published"] = True
                    published_job.job_options = opts
                    if schedule_next_pass_after_publish(published_job):
                        published_job.blocked_reason = "waiting on repository"
                        published_job.blocking_sources = []
            if plan is not None:
                from oreon_build.models import SideTag

                options = _plan_options(plan)
                side_tag = await db.get(SideTag, plan.side_tag_id) if plan.side_tag_id else None
                branch = (side_tag.name if side_tag is not None else "").strip()
                if branch:
                    arches = [published_architecture] if published_architecture else DEFAULT_ARCH
                    for arch in arches:
                        normalized_arch = normalize_machine_arch(str(arch))
                        _invalidate_reactive_repo_cache(
                            releasename, branch, normalized_arch
                        )
                        invalidate_repo_solver_cache(
                            releasename, branch, [normalized_arch]
                        )
            await evaluate_layer_buildability(
                db,
                plan_id,
                source_name=source_name,
                published_architecture=published_architecture,
            )
        await db.commit()


async def reconcile_settled_layer_plans() -> None:
    from oreon_build.db.session import async_session_maker
    from sqlalchemy import func

    async with async_session_maker() as db:
        stalled = await db.execute(
            select(LayerPlan).where(
                LayerPlan.status == LayerPlanStatus.FAILED,
                LayerPlan.error_message.ilike("Layer stalled with no queued or running jobs%"),
            )
        )
        plan_ids: list[int] = []
        for plan in stalled.scalars().all():
            n_block = await db.scalar(
                select(func.count())
                .select_from(BuildJob)
                .where(
                    BuildJob.layer_plan_id == plan.id,
                    BuildJob.status == BuildStatus.BLOCKED,
                )
            )
            if int(n_block or 0) > 0:
                plan.status = LayerPlanStatus.RUNNING
                plan.error_message = None
                plan_ids.append(int(plan.id))
        for plan_id in plan_ids:
            await evaluate_layer_buildability(db, plan_id)
        idle = await db.execute(
            select(LayerPlan).where(LayerPlan.status == LayerPlanStatus.RUNNING)
        )
        for plan in idle.scalars().all():
            if int(plan.id) in plan_ids:
                continue
            counts = await db.execute(
                select(BuildJob.status, func.count())
                .where(BuildJob.layer_plan_id == plan.id)
                .group_by(BuildJob.status)
            )
            by_status = {status: int(n) for status, n in counts.all()}
            inflight = (
                by_status.get(BuildStatus.QUEUED, 0)
                + by_status.get(BuildStatus.RUNNING, 0)
                + by_status.get(BuildStatus.PENDING, 0)
            )
            if inflight == 0 and by_status.get(BuildStatus.BLOCKED, 0) > 0:
                plan_ids.append(int(plan.id))
                await evaluate_layer_buildability(db, int(plan.id))
        await db.commit()


async def reconcile_stuck_layer_blockers() -> None:
    await reconcile_settled_layer_plans()


async def finalize_blocked_on_dead_upstreams(db: Any, plan_id: int) -> int:
    return 0


async def finalize_idle_blocked_jobs(db: Any, plan_id: int) -> int:
    return 0


async def resolve_layer_blocker_cycles(db: Any, plan_id: int, cap_index: Any = None) -> int:
    return 0


async def cancel_layer_blocker_cycles(db: Any, plan_id: int) -> int:
    return 0


async def _cheap_unblock_if_blockers_done(db: Any, job: BuildJob, plan_id: int) -> bool:
    before = job.status
    await evaluate_layer_buildability(db, int(plan_id))
    return before != job.status
