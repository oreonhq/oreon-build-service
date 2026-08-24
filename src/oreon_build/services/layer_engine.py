"""OBS-style reactive layer engine.

Replaces the old multi-wave chain planner: `seed_layer_plan` does one cheap
static-BR-only pass to seed a topological order and enqueue whatever's
already buildable against the real repo, marking the rest `blocked`.
`on_package_published` is the reactive half - a createrepo_c publish event
looks up `LayerJobBlocker` rows for the published source (indexed) and pulls
matching blocked jobs forward, evaluating dynamic BRs lazily per-package
instead of re-running the whole graph.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
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
    is_chain_plan_requirement,
    parse_versioned_requirement,
    requirement_check_capabilities,
    rpm_requirement_tokens_from_value,
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
from oreon_build.services.repo_solver import get_repo_solver

if TYPE_CHECKING:
    from oreon_build.models import Account

logger = logging.getLogger(__name__)

DEFAULT_ARCH = ["x86_64", "aarch64"]

_REACTIVE_CACHE_TTL = 300.0
_REACTIVE_CACHE_LIMIT = 2048
_REACTIVE_CACHE_LOCK = threading.RLock()
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
    if jobs:
        await db.flush()
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


async def _unlock_manifest_leaf_jobs(
    db: Any,
    jobs: list[BuildJob],
    *,
    release: Release,
    owner: str,
    repo: str,
    distgit_branch: str,
    releasename: str,
    headers: dict[str, str],
    provider_map: dict[str, set[str]],
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
        for package_name, job in package_jobs.items():
            text = await _refetch_spec_text(
                package_name,
                owner=owner,
                repo=repo,
                distgit_branch=distgit_branch,
                releasename=releasename,
                headers=headers,
            )
            if not text:
                continue
            parsed = await asyncio.to_thread(
                _spec_parse_fn(releasename, target_arches={arch}),
                text,
                f"{package_name}.spec",
            )
            if getattr(parsed, "has_generate_buildrequires", False):
                continue
            dependencies: set[str] = set()
            requirements = set(build_dependency_capabilities(parsed))
            from oreon_build.core.spec_bcond import parse_spec_bconds

            bcond = parse_spec_bconds(text, target_arches={arch})
            for conditional_requirements in bcond.conditional_brs.values():
                requirements.update(conditional_requirements)
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.lower().startswith("buildrequires:"):
                    value = stripped.split(":", 1)[1].strip()
                    requirements.update(rpm_requirement_tokens_from_value(value))
            for requirement in requirements:
                for source in _layer_sources_for_requirement(str(requirement), provider_map):
                    key = source.strip().lower()
                    if key in package_jobs and key != package_name:
                        dependencies.add(key)
            logger.info(
                "manifest leaf check package=%s arch=%s dependencies=%s",
                package_name,
                arch,
                sorted(dependencies),
            )
            if dependencies:
                job.blocked_reason = "waiting on layer jobs: " + ", ".join(sorted(dependencies)[:8])
                continue
            job.status = BuildStatus.QUEUED
            job.blocked_reason = None


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
    owner_repo = _github_owner_repo_from_git_url(distgit_url)
    if owner_repo:
        owner, repo = owner_repo
        provider_index = await get_distgit_provides_index_cached(
            owner=owner,
            repo=repo,
            distgit_branch=distgit_branch,
            releasename=(release.releasename or "").strip(),
            headers=_github_headers(),
        )
        provider_map = provider_index.indexed_providers() if provider_index is not None else {}
        await _unlock_manifest_leaf_jobs(
            db,
            jobs,
            release=release,
            owner=owner,
            repo=repo,
            distgit_branch=distgit_branch,
            releasename=(release.releasename or "").strip(),
            headers=_github_headers(),
            provider_map=provider_map,
        )
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
    return plan, jobs


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
    candidates = sorted({
        source
        for capability in missing
        for source in _layer_sources_for_requirement(capability, providers)
        if source
    })
    if not candidates:
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
    if depth >= _REACTIVE_EXPANSION_MAX_DEPTH:
        return []
    existing_rows = await db.execute(
        select(Package.name)
        .join(BuildJob, BuildJob.package_id == Package.id)
        .where(BuildJob.layer_plan_id == int(plan_id))
    )
    existing = {str(name) for (name,) in existing_rows.all() if name}
    selected = next(
        (candidate for candidate in candidates if candidate not in existing and candidate not in visited),
        None,
    )
    if selected is None:
        return []
    visited.add(selected)
    current["expanded_sources"] = sorted(visited)
    current["expansion_depth"] = depth + 1
    locked_plan.plan_options = current
    locked_plan.root_packages = list(locked_plan.root_packages or []) + [selected]
    new_jobs = await _create_job_for_package(
        db, locked_plan, selected, release=release, architectures=architectures,
        distgit_url=distgit_url, distgit_branch=distgit_branch,
        preferred_mock_environment_id=options.get("preferred_mock_environment_id"),
        priority=int(options.get("priority") or 0), triggered_by_id=locked_plan.triggered_by_id,
        request=None, releasename=releasename, publish_branch=side_tag_name, layer_index=0,
        initial_status=BuildStatus.BLOCKED, blocked_reason="waiting on repository",
    )
    await db.flush()
    return new_jobs


async def _update_layer_job_if_active(
    db: Any,
    job_id: int,
    plan_id: int,
    *,
    status: BuildStatus | None = None,
    blocked_reason: str | None = None,
) -> bool:
    values: dict[str, Any] = {}
    if status is not None:
        values["status"] = status
    if blocked_reason is not None:
        values["blocked_reason"] = blocked_reason[:500]
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


def _layer_sources_for_requirement(
    requirement: str,
    provider_map: dict[str, set[str]],
) -> set[str]:
    sources: set[str] = set()
    for capability in requirement_check_capabilities(requirement):
        sources.update(provider_map.get(capability, set()))
        sources.update(provider_map.get(capability.lower(), set()))
        primary = parse_versioned_requirement(capability)[0]
        if primary:
            sources.update(provider_map.get(primary, set()))
            sources.update(provider_map.get(primary.lower(), set()))
            sources.add(primary)
    return {source for source in sources if source}


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


async def _evaluate_layer_buildability(db: Any, layer_plan_id: int) -> list[BuildJob]:
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
    rows = await db.execute(
        select(BuildJob, BuildTarget.architecture)
        .join(BuildTarget, BuildTarget.id == BuildJob.target_id)
        .where(
            BuildJob.layer_plan_id == plan.id,
            BuildJob.status.in_((BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.BLOCKED)),
        )
    )
    changed: list[BuildJob] = []
    expanded = False
    provider_index = await get_distgit_provides_index_cached(
        owner=owner, repo=repo, distgit_branch=distgit_branch,
        releasename=releasename, headers=_github_headers(),
    )
    provider_map = provider_index.indexed_providers() if provider_index is not None else {}
    layer_rows = await db.execute(
        select(Package.name, BuildJob.status)
        .join(BuildJob, BuildJob.package_id == Package.id)
        .where(BuildJob.layer_plan_id == plan.id)
    )
    layer_statuses: dict[str, set[BuildStatus]] = {}
    for name, job_status in layer_rows.all():
        if name:
            layer_statuses.setdefault(str(name).strip().lower(), set()).add(job_status)
    from oreon_build.services.distgit_order import enrich_specs_buildrequires_with_rpm
    from oreon_build.models import SideTag
    side_tag = await db.get(SideTag, plan.side_tag_id) if plan.side_tag_id else None
    side_tag_name = side_tag.name if side_tag is not None else str(options.get("branch") or "")
    architectures = [str(value) for value in (options.get("architectures") or DEFAULT_ARCH) if value] or list(DEFAULT_ARCH)
    for job, architecture in rows.all():
        await db.refresh(plan)
        if plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
            break
        arch = normalize_machine_arch(str(architecture or ""))
        package = await db.get(Package, job.package_id)
        if not arch or package is None:
            continue
        text = await _refetch_spec_text(
            package.name, owner=owner, repo=repo, distgit_branch=distgit_branch,
            releasename=releasename, headers=_github_headers(),
        )
        if not text:
            await _update_layer_job_if_active(
                db, job.id, plan.id, blocked_reason="waiting on repository: source unavailable"
            )
            continue
        spec_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        req_key = (owner + "/" + repo, distgit_branch, package.name, arch, spec_digest)
        cached_req = _reactive_cache_get(_REACTIVE_REQUIREMENTS_CACHE, req_key)
        if cached_req is not None:
            requirements, complete = cached_req
            parsed = None
        else:
            parsed = await asyncio.to_thread(
                _spec_parse_fn(releasename, target_arches={arch}), text, f"{package.name}.spec"
            )
            await enrich_specs_buildrequires_with_rpm(
                {package.name: text}, {package.name: parsed}, releasename=releasename,
                scope_packages={package.name},
                generate_packages={package.name} if getattr(parsed, "has_generate_buildrequires", False) else set(),
                owner=owner, repo=repo, distgit_branch=distgit_branch, headers=_github_headers(),
            )
            requirements = tuple(build_dependency_capabilities(parsed))
            complete = not getattr(parsed, "has_generate_buildrequires", False) or getattr(parsed, "dyn_br_complete", None) is True
            _reactive_cache_put(_REACTIVE_REQUIREMENTS_CACHE, req_key, (requirements, complete))
        if not complete:
            await _update_layer_job_if_active(
                db, job.id, plan.id, blocked_reason="waiting on repository: dynamic buildrequires"
            )
            continue
        cache_key = (releasename, job.branch, arch)
        cached_solver = _reactive_cache_get(_REACTIVE_SOLVER_CACHE, cache_key)
        if cached_solver is None:
            solver = await asyncio.to_thread(
                get_repo_solver, releasename, job.branch, [arch],
                extra_branches=default_capability_extra_branches(job.branch),
            )
            cached_solver = (solver, InstallOracle(solver))
            _reactive_cache_put(_REACTIVE_SOLVER_CACHE, cache_key, cached_solver)
        _, oracle = cached_solver
        oracle.clear_overlay()
        result = await asyncio.to_thread(oracle.resolve, requirements)
        missing = _solver_missing_caps(result)
        layer_blockers = {
            source
            for requirement in requirements
            for source in _layer_sources_for_requirement(str(requirement), provider_map)
            if source.strip().lower() in layer_statuses
            and not all(
                status in (BuildStatus.SUCCESS, BuildStatus.SKIPPED)
                for status in layer_statuses[source.strip().lower()]
            )
        }
        if missing and provider_index is not None:
            added_jobs = await _expand_missing_provider(
                db, int(plan.id), missing, plan=plan, release=release,
                side_tag_name=side_tag_name, architectures=architectures, options=options,
                distgit_url=distgit_url, distgit_branch=distgit_branch, owner=owner, repo=repo,
                releasename=releasename, provider_index=provider_index,
            )
            expanded = expanded or bool(added_jobs)
        if result.ok and not missing and not layer_blockers:
            if job.status != BuildStatus.QUEUED:
                changed_now = await _update_layer_job_if_active(
                    db, job.id, plan.id, status=BuildStatus.QUEUED, blocked_reason=""
                )
                if changed_now:
                    changed.append(job)
            else:
                await _update_layer_job_if_active(db, job.id, plan.id, blocked_reason="")
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
            )
    await db.flush()
    if expanded:
        changed.extend(await _evaluate_layer_buildability(db, int(plan.id)))
    return changed



async def evaluate_layer_buildability(db: Any, layer_plan_id: int) -> list[BuildJob]:
    plan_id = int(layer_plan_id)
    lock = _REACTIVE_PLAN_LOCKS.get(plan_id)
    if lock is None:
        lock = asyncio.Lock()
        _REACTIVE_PLAN_LOCKS[plan_id] = lock
    async with lock:
        return await _evaluate_layer_buildability(db, plan_id)



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
                from oreon_build.models import SideTag

                options = _plan_options(plan)
                side_tag = await db.get(SideTag, plan.side_tag_id) if plan.side_tag_id else None
                branch = (side_tag.name if side_tag is not None else "").strip()
                if branch:
                    arches = [published_architecture] if published_architecture else DEFAULT_ARCH
                    for arch in arches:
                        _invalidate_reactive_repo_cache(
                            releasename, branch, normalize_machine_arch(str(arch))
                        )
            await evaluate_layer_buildability(db, plan_id)
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
