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
from oreon_build.services.dep_plan.install_oracle import InstallOracle
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
from oreon_build.services.repo_solver import get_repo_solver, invalidate_repo_solver_cache

if TYPE_CHECKING:
    from oreon_build.models import Account

logger = logging.getLogger(__name__)

DEFAULT_ARCH = ["x86_64", "aarch64"]

_REACTIVE_CACHE_TTL = 300.0
_REACTIVE_CACHE_LIMIT = 2048
_REACTIVE_REQUIREMENTS_CACHE_VERSION = "dynamic-br-v6"
_REACTIVE_CACHE_LOCK = threading.RLock()
_LAYER_EVAL_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="layer-eval")
_REACTIVE_SPEC_CACHE: dict[tuple[str, str, str, str], tuple[float, str]] = {}
_REACTIVE_REQUIREMENTS_CACHE: dict[tuple[str, str, str, str, str], tuple[float, tuple[str, ...], bool]] = {}
_REACTIVE_INDEX_CACHE: dict[tuple[str, str, str], tuple[float, RepoCapabilityIndex]] = {}
_REACTIVE_SOLVER_CACHE: dict[tuple[str, str, str], tuple[float, tuple[Any, InstallOracle]]] = {}
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


_RE_NOTHING_PROVIDES_REQUESTED = re.compile(
    r"nothing provides requested\s+(?P<cap>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_missing_provides_from_text(text: str) -> set[str]:
    out: set[str] = set()
    raw = text or ""
    token_re = re.compile(r"[A-Za-z_][A-Za-z0-9_+:.@/-]*(?:\([^()]*\))?")
    operators = {"and", "or", "with", "without"}
    for m in _RE_NOTHING_PROVIDES_REQUESTED.finditer(raw):
        cap = (m.group("cap") or "").strip().rstrip(".")
        if not cap or "%" in cap:
            continue
        if cap.startswith("(") and cap.endswith(")"):
            candidates = token_re.findall(cap[1:-1])
        else:
            candidates = [cap]
        for candidate in candidates:
            token = candidate.strip().rstrip(".")
            if (
                token.lower() in operators
                or token in {"<", ">", "=", "<=", ">=", "!=", "=="}
                or token[0].isdigit()
            ):
                continue
            if token and "%" not in token:
                out.add(token)
    return out


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


async def _refetch_spec_text(
    pkg_name: str, *, owner: str, repo: str, distgit_branch: str, releasename: str, headers: dict[str, str]
) -> str | None:
    key = (owner + "/" + repo, distgit_branch, releasename, pkg_name)
    cached = _reactive_cache_get(_REACTIVE_SPEC_CACHE, key)
    if cached is not None:
        return cached
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), headers=headers) as client:
        batch = await _fetch_distgit_specs_batch(
            client, owner=owner, repo=repo, distgit_branch=distgit_branch, releasename=releasename, packages=[pkg_name]
        )
    text = batch.get(pkg_name)
    if text:
        _reactive_cache_put(_REACTIVE_SPEC_CACHE, key, text)
    return text


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
) -> list[tuple[str, int]]:
    """Best provider per missing capability, verified against version bounds.

    Candidates violating any explicit bound are dropped. Survivors ranked by
    how many missing capabilities they cover.
    """
    per_candidate_reqs: dict[str, list[str]] = {}
    for capability in sorted(missing):
        sources = _layer_sources_for_requirement(capability, providers)
        for source in sources:
            reqs = per_candidate_reqs.setdefault(source, [])
            if capability not in reqs:
                reqs.append(capability)
    if not per_candidate_reqs:
        return []
    verified: list[tuple[str, int]] = []
    check_gate = asyncio.Semaphore(8)

    async def verify(source: str, requirements: list[str]) -> None:
        async with check_gate:
            ok = await _candidate_satisfies_requirements(
                source, requirements, owner=owner, repo=repo,
                distgit_branch=distgit_branch, releasename=releasename, headers=headers,
            )
        if ok:
            verified.append((source, len(requirements)))

    await asyncio.gather(
        *(verify(source, requirements) for source, requirements in sorted(per_candidate_reqs.items()))
    )
    verified.sort(key=lambda item: (-item[1], item[0]))
    return verified


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
        missing, providers, owner=owner, repo=repo,
        distgit_branch=distgit_branch, releasename=releasename, headers=_github_headers(),
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
        if candidate not in existing and candidate not in visited and depth + 1 <= _REACTIVE_EXPANSION_MAX_DEPTH
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
                    if str(value).strip() and _canonical_layer_name(str(value)) in names
                )
        canon = _canonical_layer_name(primary)
        if canon in names:
            sources.add(canon)
    sources.update(
        _canonical_layer_name(source)
        for source in providers_for_requirement(
            requirement,
            provider_map=provider_map,
            package_names=names,
        )
        if _canonical_layer_name(source) in names
    )
    return sources


def _incomplete_layer_sources(
    requirements: tuple[str, ...] | set[str] | list[str],
    provider_map: dict[str, set[str]],
    package_names: set[str],
    layer_statuses: dict[tuple[str, str], set[BuildStatus]],
    architecture: str,
) -> set[str]:
    arch = normalize_machine_arch(str(architecture or ""))
    out: set[str] = set()
    for requirement in requirements:
        for source in _layer_sources_for_requirement(str(requirement), provider_map, package_names):
            name = _canonical_layer_name(source)
            statuses = layer_statuses.get((name, arch), set())
            if statuses and not all(status in (BuildStatus.SUCCESS, BuildStatus.SKIPPED) for status in statuses):
                out.add(name)
    return out


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


async def _start_bootstrap_pass(db: Any, job: BuildJob, dropped_sources: set[str]) -> bool:
    dropped = sorted({_canonical_layer_name(source) for source in dropped_sources if str(source).strip()})
    if not dropped:
        return False
    opts = job_options_dict(job)
    if str(opts.get("build_profile") or "full").strip().lower() == "full" and opts.get("bootstrap_dropped"):
        return False
    opts["build_profile"] = "bootstrap"
    opts["rpmbuild_defines"] = {"with_check": "0"}
    opts["next_profile"] = "full"
    opts["bootstrap_dropped"] = dropped
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
    if plan is None or plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
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
    graph_rows_result = await db.execute(
        select(BuildJob, BuildTarget.architecture)
        .join(BuildTarget, BuildTarget.id == BuildJob.target_id)
        .where(BuildJob.layer_plan_id == plan.id)
    )
    graph_row_list = list(graph_rows_result.all())
    changed: list[BuildJob] = []
    expanded = False
    provider_index = await get_distgit_provides_index_cached(
        owner=owner, repo=repo, distgit_branch=distgit_branch,
        releasename=releasename, headers=_github_headers(),
    )
    provider_map = provider_index.indexed_providers() if provider_index is not None else {}
    provider_package_names = set(getattr(provider_index, "package_names", ()) or ())
    layer_rows_result = await db.execute(
        select(Package.name, BuildJob.status, BuildTarget.architecture)
        .join(BuildJob, BuildJob.package_id == Package.id)
        .join(BuildTarget, BuildTarget.id == BuildJob.target_id)
        .where(BuildJob.layer_plan_id == plan.id)
    )
    layer_rows = list(layer_rows_result.all())
    layer_statuses: dict[tuple[str, str], set[BuildStatus]] = {}
    layer_package_names: set[str] = set()
    for name, job_status, job_arch in layer_rows:
        if name:
            canonical_name = _canonical_layer_name(str(name))
            layer_package_names.add(canonical_name)
            arch_key = normalize_machine_arch(str(job_arch or ""))
            layer_statuses.setdefault((canonical_name, arch_key), set()).add(job_status)
    from oreon_build.services.distgit_order import enrich_specs_buildrequires_with_rpm
    from oreon_build.models import SideTag
    side_tag = await db.get(SideTag, plan.side_tag_id) if plan.side_tag_id else None
    side_tag_name = side_tag.name if side_tag is not None else str(options.get("branch") or "")
    architectures = [str(value) for value in (options.get("architectures") or DEFAULT_ARCH) if value] or list(DEFAULT_ARCH)
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
    prepared: dict[tuple[int, str], tuple[Package, str | None, Any | None, tuple[str, ...], bool]] = {}
    prepare_gate = asyncio.Semaphore(16)

    async def prepare_job(job: BuildJob, architecture: str) -> None:
        arch = normalize_machine_arch(str(architecture or ""))
        package = await db.get(Package, job.package_id)
        if not arch or package is None:
            return
        async with prepare_gate:
            text = await _refetch_spec_text(
                package.name, owner=owner, repo=repo, distgit_branch=distgit_branch,
                releasename=releasename, headers=_github_headers(),
            )
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
                await enrich_specs_buildrequires_with_rpm(
                    {package.name: text}, {package.name: parsed}, releasename=releasename,
                    scope_packages={package.name},
                    generate_packages={package.name}
                    if _manifest_has_dynamic_buildrequires(text, parsed)
                    else set(),
                    owner=owner, repo=repo, distgit_branch=distgit_branch, headers=_github_headers(),
                    defines_by_package={package.name: profile_defines} if profile_defines else None,
                )
                requirements = tuple(sorted(_manifest_build_requirements(
                    text, parsed, architecture=arch, defines=profile_defines or None,
                    releasename=releasename,
                )))
                complete = (
                    not _manifest_has_dynamic_buildrequires(text, parsed)
                    or getattr(parsed, "dyn_br_complete", None) is True
                )
                _reactive_cache_put(_REACTIVE_REQUIREMENTS_CACHE, req_key, (requirements, complete))
            prepared[(int(job.id), arch)] = (package, text, parsed, tuple(requirements), complete)

    await asyncio.gather(*(prepare_job(job, architecture) for job, architecture in graph_row_list))
    bootstrap_drops: dict[tuple[int, str], set[str]] = {}
    for architecture in {normalize_machine_arch(str(value or "")) for _, value in graph_row_list}:
        arch_rows = [
            (job, prepared.get((int(job.id), normalize_machine_arch(str(arch or "")))) )
            for job, arch in graph_row_list
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
    failure_cascaded = False
    for job, architecture in row_list:
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
            await _start_bootstrap_pass(db, job, dropped_sources)
            opts = job_options_dict(job)
        elif dropped_sources and not opts.get("bootstrap_dropped"):
            await _start_bootstrap_pass(db, job, dropped_sources)
            opts = job_options_dict(job)
        if str(opts.get("build_profile") or "full").strip().lower() == "bootstrap":
            dropped_sources = {
                _canonical_layer_name(value)
                for value in (opts.get("bootstrap_dropped") or dropped_sources)
                if str(value).strip()
            }
            if text and parsed is not None:
                profile_defines = dict(opts.get("rpmbuild_defines") or {"with_check": "0"})
                if _manifest_has_dynamic_buildrequires(text, parsed):
                    await enrich_specs_buildrequires_with_rpm(
                        {package.name: text}, {package.name: parsed}, releasename=releasename,
                        scope_packages={package.name},
                        generate_packages={package.name},
                        owner=owner, repo=repo, distgit_branch=distgit_branch, headers=_github_headers(),
                        defines_by_package={package.name: profile_defines},
                    )
                requirements = tuple(sorted(_manifest_build_requirements(
                    text, parsed, architecture=arch,
                    defines=profile_defines,
                    releasename=releasename,
                )))
                complete = (
                    not _manifest_has_dynamic_buildrequires(text, parsed)
                    or getattr(parsed, "dyn_br_complete", None) is True
                )
        effective_requirements = tuple(requirements)
        stored_blockers = {
            _canonical_layer_name(value)
            for value in (job.blocking_sources or [])
            if str(value).strip()
        }
        if source_filter:
            stored_blockers.discard(source_filter)
        if not text:
            await _update_layer_job_if_active(
                db, job.id, plan.id, blocked_reason="waiting on repository: source unavailable"
            )
            continue
        if not complete:
            await _update_layer_job_if_active(
                db, job.id, plan.id, blocked_reason="waiting on repository: dynamic buildrequires"
            )
            continue
        layer_blockers = set(stored_blockers)
        layer_blockers.update(_incomplete_layer_sources(
            effective_requirements, provider_map, layer_package_names, layer_statuses, arch
        ))
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
        cache_key = (releasename, job.branch, arch)
        cached_solver = _reactive_cache_get(_REACTIVE_SOLVER_CACHE, cache_key)
        if cached_solver is None:
            solver = await _layer_to_thread(
                get_repo_solver, releasename, job.branch, [arch],
                extra_branches=default_capability_extra_branches(job.branch),
            )
            cached_solver = (solver, InstallOracle(solver))
            _reactive_cache_put(_REACTIVE_SOLVER_CACHE, cache_key, cached_solver)
        _, oracle = cached_solver
        oracle.clear_overlay()
        real_result = await _layer_to_thread(oracle.resolve, effective_requirements)
        missing = _solver_missing_caps(real_result)
        if missing and provider_index is not None:
            added_jobs = await _expand_missing_provider(
                db, int(plan.id), missing, plan=plan, release=release,
                side_tag_name=side_tag_name, architectures=architectures, options=options,
                distgit_url=distgit_url, distgit_branch=distgit_branch, owner=owner, repo=repo,
                releasename=releasename, provider_index=provider_index,
            )
            expanded = expanded or bool(added_jobs)
        if missing:
            layer_blockers.update(_incomplete_layer_sources(
                tuple(sorted(missing)), provider_map, layer_package_names, layer_statuses, arch
            ))
        full_pass = str(opts.get("build_profile") or "full").strip().lower() == "full"
        bootstrap_history = bool(opts.get("bootstrap_dropped"))
        if missing and (layer_blockers or expanded):
            await _update_layer_job_if_active(
                db,
                job.id,
                plan.id,
                status=BuildStatus.BLOCKED,
                blocked_reason="waiting on layer jobs: " + ", ".join(sorted(layer_blockers or missing)[:8]),
                blocking_sources=sorted(layer_blockers),
            )
            continue
        if missing and full_pass and bootstrap_history and not layer_blockers:
            failed_now = await _update_layer_job_if_active(
                db,
                job.id,
                plan.id,
                status=BuildStatus.FAILED,
                blocked_reason="full pass unresolved: " + ", ".join(sorted(missing)[:8]),
                blocking_sources=[],
                error_message="Full pass unresolved after bootstrap: " + ", ".join(sorted(missing)[:8]),
                completed_at=datetime.now(timezone.utc),
            )
            failure_cascaded = failure_cascaded or failed_now
            continue
        if real_result.ok and not missing and not layer_blockers:
            if job.status != BuildStatus.QUEUED:
                changed_now = await _update_layer_job_if_active(
                    db, job.id, plan.id, status=BuildStatus.QUEUED, blocked_reason="", blocking_sources=[]
                )
                if changed_now:
                    changed.append(job)
            else:
                await _update_layer_job_if_active(db, job.id, plan.id, blocked_reason="", blocking_sources=[])
        else:
            details = set(missing)
            details.update(f"{source} (layer job)" for source in layer_blockers)
            detail = ", ".join(sorted(details)) or "dependency solver failure"
            await _update_layer_job_if_active(
                db,
                job.id,
                plan.id,
                status=BuildStatus.BLOCKED,
                blocked_reason=f"waiting on repository: {detail}",
                blocking_sources=sorted(layer_blockers),
            )
    await db.flush()
    if failure_cascaded:
        changed.extend(await _evaluate_layer_buildability(db, int(plan.id)))
    if expanded:
        changed.extend(await _evaluate_layer_buildability(db, int(plan.id)))
    if not expanded and not failure_cascaded:
        final_rows = await db.execute(
            select(BuildJob.status).where(BuildJob.layer_plan_id == plan.id)
        )
        final_statuses = [status for (status,) in final_rows.all()]
        active_count = sum(
            status in (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)
            for status in final_statuses
        )
        failed_count = sum(status == BuildStatus.FAILED for status in final_statuses)
        blocked_count = sum(status == BuildStatus.BLOCKED for status in final_statuses)
        if active_count == 0 and blocked_count == 0 and failed_count > 0:
            plan.status = LayerPlanStatus.FAILED
            plan.error_message = (
                "Layer failed: upstream package failure left no active jobs"
            )
            await db.flush()
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
        return await _evaluate_layer_buildability(
            db,
            plan_id,
            source_name=source_name,
            published_architecture=published_architecture,
        )



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
    job.status = BuildStatus.BLOCKED
    job.completed_at = None
    job.blocked_reason = "waiting on repository: dynamic buildrequires"
    await db.flush()
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
                LayerPlan.status.in_((LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING)),
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
    return


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
