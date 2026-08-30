"""OBS-style reactive layer scheduler.

Seed blocked. SAT against published repos. Queue when SAT installs. Wait or
heal only on caps SAT actually missed. No stub Provides. No cycle bypass.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status
from sqlalchemy import exists, select, update

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
    Package,
    Release,
    SideTag,
)
from oreon_build.services.build_pass import (
    job_options_dict,
    schedule_next_pass_after_publish,
)
from oreon_build.core.rpm_requires import (
    evr_satisfies,
    expand_version_macros,
    is_spurious_solver_miss,
    parse_versioned_requirement,
    requirement_check_capabilities,
    requirement_usable_for_solver,
    split_requirement_evr,
)
from oreon_build.services.build_target_util import get_or_create_build_target, resolve_mock_environment_id
from oreon_build.services.dep_plan.install_oracle import InstallOracle
from oreon_build.services.dep_plan.providers import (
    filter_providers_for_consumer,
    is_base_system_package,
    narrow_alternate_providers,
    providers_for_requirement,
    should_seed_providers,
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
    repo_channels_from_mock_urls,
)
from oreon_build.services.repo_solver import get_repo_solver, invalidate_repo_solver_cache

if TYPE_CHECKING:
    from oreon_build.models import Account

logger = logging.getLogger(__name__)

DEFAULT_ARCH = ["x86_64", "aarch64"]

_REACTIVE_CACHE_TTL = 1800.0
_REACTIVE_CACHE_LIMIT = 8192
_REACTIVE_REQUIREMENTS_CACHE_VERSION = "sat-br-v14"
_REACTIVE_CACHE_LOCK = threading.RLock()
_LAYER_EVAL_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="layer-eval")
_SPEC_UNPARSED = object()
_REACTIVE_SPEC_CACHE: dict[tuple[str, str, str, str], tuple[float, str]] = {}
_REACTIVE_REQUIREMENTS_CACHE: dict[tuple[str, str, str, str, str], tuple[float, tuple[str, ...], bool]] = {}
_REACTIVE_SOLVER_CACHE: dict[tuple[str, str, str], tuple[float, Any]] = {}
_REACTIVE_PLAN_LOCKS: dict[int, asyncio.Lock] = {}
_REACTIVE_PLAN_LOCK_OWNERS: dict[int, int] = {}


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


def _prepare_spec_requirements(
    spec_text: str,
    package_name: str,
    releasename: str,
    architecture: str,
    defines: dict[str, str] | None,
) -> tuple[Any | None, tuple[str, ...], bool]:
    parsed = _spec_parse_fn(releasename, target_arches={architecture})(
        spec_text, f"{package_name}.spec"
    )
    from oreon_build.core.spec_bcond import default_distro_macros
    from oreon_build.services.dep_plan.br_oracle import evaluate_package_brs

    reqs: set[str] = set()
    complete = True
    try:
        br = evaluate_package_brs(
            spec_text,
            pkg=package_name,
            distro_macros=default_distro_macros(releasename),
            target_arches={architecture},
            allow_source_download=True,
        )
        reqs.update(str(item).strip() for item in (br.buildrequires or ()) if str(item).strip())
        complete = bool(br.complete)
    except Exception:
        logger.exception("br oracle failed package=%s arch=%s", package_name, architecture)
        complete = not _manifest_has_dynamic_buildrequires(spec_text, parsed)
    reqs.update(
        _manifest_build_requirements(
            spec_text,
            parsed,
            architecture=architecture,
            defines=defines,
            releasename=releasename,
        )
    )
    rpm_version = _spec_rpm_version(spec_text, parsed)
    kept: set[str] = set()
    for requirement in reqs:
        expanded = expand_version_macros(requirement, rpm_version) if rpm_version else requirement
        usable = requirement_usable_for_solver(expanded)
        if usable:
            kept.add(usable)
    return parsed, tuple(sorted(kept)), complete


def _invalidate_reactive_repo_cache(releasename: str, branch: str, arch: str) -> None:
    with _REACTIVE_CACHE_LOCK:
        for key in list(_REACTIVE_SOLVER_CACHE):
            if len(key) >= 3 and key[0] == releasename and key[1] == branch and key[2] == arch:
                _REACTIVE_SOLVER_CACHE.pop(key, None)


def clear_reactive_solver_cache() -> None:
    with _REACTIVE_CACHE_LOCK:
        _REACTIVE_SOLVER_CACHE.clear()


def invalidate_reactive_requirements_for_package(package_name: str) -> None:
    want = _canonical_layer_name(package_name)
    if not want:
        return
    with _REACTIVE_CACHE_LOCK:
        for key in list(_REACTIVE_REQUIREMENTS_CACHE):
            if len(key) >= 4 and _canonical_layer_name(str(key[3])) == want:
                _REACTIVE_REQUIREMENTS_CACHE.pop(key, None)


def parse_missing_provides_from_text(text: str) -> set[str]:
    return set(parse_dnf_missing_provides(text))


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


def _spec_rpm_version(spec_text: str, parsed: Any) -> str:
    for attr in ("version", "upstream_version", "rpm_version"):
        val = str(getattr(parsed, attr, "") or "").strip()
        if val and "%" not in val:
            return val
    for line in (spec_text or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("version:"):
            val = stripped.split(":", 1)[1].strip().split()[0] if ":" in stripped else ""
            if val and "%" not in val:
                return val
    return ""


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
    from oreon_build.services.dynamic_brs import rpmspec_query_buildrequires

    try:
        extra = rpmspec_query_buildrequires(spec_text, defines=merged)
    except Exception:
        extra = []
    requirements.update(str(item).strip() for item in extra if str(item).strip())
    rpm_version = _spec_rpm_version(spec_text, parsed)
    kept: set[str] = set()
    for requirement in requirements:
        expanded = expand_version_macros(requirement, rpm_version) if rpm_version else requirement
        usable = requirement_usable_for_solver(expanded)
        if usable:
            kept.add(usable)
    return kept


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
        if not ready.get((name, arch), False):
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


def _heal_sources_for_requirement(
    requirement: str,
    provider_map: dict[str, set[str]],
    distgit_names: set[str],
    consumer: str,
) -> set[str]:
    known = {_canonical_layer_name(name) for name in distgit_names if str(name).strip()}
    raw = {
        _canonical_layer_name(source)
        for source in providers_for_requirement(
            requirement,
            provider_map=provider_map,
            package_names=known,
        )
        if str(source).strip()
    }
    raw &= known
    raw = {name for name in raw if not is_base_system_package(name)}
    if not raw:
        from oreon_build.services.virtual_resolve import import_suggestion_for_cap

        suggestion = import_suggestion_for_cap(requirement)
        canon = _canonical_layer_name(suggestion or "")
        if canon in known and not is_base_system_package(canon):
            raw = {canon}
    if not raw or not should_seed_providers(consumer, requirement, raw):
        return set()
    raw = {
        _canonical_layer_name(name)
        for name in filter_providers_for_consumer(consumer, raw)
        if _canonical_layer_name(name) in known
    }
    raw = {
        _canonical_layer_name(name)
        for name in narrow_alternate_providers(requirement, raw)
        if _canonical_layer_name(name) in known
    }
    return raw


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
    consumer: str = "",
) -> list[tuple[str, int]]:
    names = {_canonical_layer_name(str(name)) for name in (package_names or set()) if str(name).strip()}
    orig_by_lower = {
        _canonical_layer_name(str(name)): str(name).strip()
        for name in (package_names or set())
        if str(name).strip()
    }
    per_candidate_reqs: dict[str, list[str]] = {}
    for capability in sorted(missing):
        sources = _heal_sources_for_requirement(str(capability), providers, names, consumer)
        for source in sources:
            real = orig_by_lower.get(_canonical_layer_name(source))
            if not real:
                continue
            reqs = per_candidate_reqs.setdefault(real, [])
            if capability not in reqs:
                reqs.append(capability)
    ranked = sorted(
        ((source, len(requirements), requirements) for source, requirements in per_candidate_reqs.items()),
        key=lambda item: (-item[1], item[0]),
    )
    out: list[tuple[str, int]] = []
    for source, score, requirements in ranked:
        text = await _refetch_spec_text(
            source,
            owner=owner,
            repo=repo,
            distgit_branch=distgit_branch,
            releasename=releasename,
            headers=headers,
        )
        if not text:
            continue
        needs_evr = any(
            parse_versioned_requirement(str(req))[1] and "(" not in (parse_versioned_requirement(str(req))[0] or "")
            for req in requirements
        )
        if needs_evr and not await _candidate_satisfies_requirements(
            source,
            requirements,
            owner=owner,
            repo=repo,
            distgit_branch=distgit_branch,
            releasename=releasename,
            headers=headers,
        ):
            continue
        out.append((source, score))
    return out


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
    headers: dict[str, str] | None = None,
    consumer: str = "",
) -> tuple[list[BuildJob], set[str]]:
    providers = provider_index.indexed_providers() if provider_index is not None else {}
    distgit_names = set(getattr(provider_index, "package_names", None) or ())
    locked = await db.execute(
        select(LayerPlan).where(LayerPlan.id == int(plan_id))
    )
    locked_plan = locked.scalar_one_or_none()
    if locked_plan is None or locked_plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
        return [], set()
    existing_rows = await db.execute(
        select(Package.name)
        .join(BuildJob, BuildJob.package_id == Package.id)
        .where(BuildJob.layer_plan_id == int(plan_id))
    )
    existing = {str(name) for (name,) in existing_rows.all() if name}
    existing_canon = {_canonical_layer_name(name) for name in existing}
    universe = distgit_names or existing
    verified = await _pick_expansion_candidates(
        set(missing), providers, owner=owner, repo=repo,
        distgit_branch=distgit_branch, releasename=releasename,
        headers=headers or _github_headers(),
        package_names=universe,
        consumer=consumer,
    )
    if not verified:
        return [], set()
    wait_on = {_canonical_layer_name(candidate) for candidate, _score in verified}
    wait_on.discard(_canonical_layer_name(consumer))
    current = dict(locked_plan.plan_options or {})
    visited = {_canonical_layer_name(str(value)) for value in (current.get("expanded_sources") or []) if str(value).strip()}
    selected = [
        candidate
        for candidate, _score in verified
        if candidate not in existing
        and _canonical_layer_name(candidate) not in existing_canon
        and _canonical_layer_name(candidate) not in visited
        and not is_base_system_package(_canonical_layer_name(candidate))
    ]
    if not selected:
        return [], wait_on
    visited.update(_canonical_layer_name(name) for name in selected)
    current["expanded_sources"] = sorted(visited)
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
    wait_on |= {_canonical_layer_name(name) for name in selected}
    wait_on.discard(_canonical_layer_name(consumer))
    return new_jobs, wait_on


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
    usable = requirement_usable_for_solver(requirement)
    if not usable:
        return set()
    requirement = usable
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
        if canon and "(" not in canon and not canon.startswith("/") and (
            not restrict or canon in names
        ):
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
    return {
        _canonical_layer_name(source)
        for source in narrow_alternate_providers(requirement, sources)
        if _canonical_layer_name(source)
    }


def _job_repo_published(status: BuildStatus, job_options: dict[str, Any] | None) -> bool:
    if status == BuildStatus.SKIPPED:
        return True
    if status != BuildStatus.SUCCESS:
        return False
    if not isinstance(job_options, dict):
        return False
    return bool(job_options.get("repo_published"))


def _sat_hold_caps_from_job(job: Any) -> tuple[str, ...]:
    opts = job_options_dict(job)
    raw = opts.get("sat_hold_caps")
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        cap = str(item or "").strip()
        if not cap or cap in seen:
            continue
        seen.add(cap)
        out.append(cap)
    return tuple(out)


def _merge_requirement_caps(*groups: tuple[str, ...] | list[str] | set[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            cap = str(item or "").strip()
            if not cap or cap in seen:
                continue
            seen.add(cap)
            out.append(cap)
    return tuple(out)


def _hold_caps_layer_sources(
    hold: tuple[str, ...] | list[str],
    provider_map: dict[str, set[str]],
    package_names: set[str],
    consumer: str,
) -> set[str]:
    mapped: set[str] = set()
    skip = _canonical_layer_name(consumer)
    for cap in hold:
        for source in _layer_sources_for_requirement(str(cap), provider_map, package_names):
            name = _canonical_layer_name(source)
            if name and name != skip:
                mapped.add(name)
    return mapped


def _hold_caps_ready_on_layer(
    hold: tuple[str, ...] | list[str],
    provider_map: dict[str, set[str]],
    package_names: set[str],
    layer_repo_ready: dict[tuple[str, str], bool],
    architecture: str,
    consumer: str,
) -> bool:
    if not hold:
        return True
    arch = normalize_machine_arch(str(architecture or ""))
    mapped = _hold_caps_layer_sources(hold, provider_map, package_names, consumer)
    if not mapped:
        return False
    return all(layer_repo_ready.get((name, arch), False) for name in mapped)


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
            if not ready.get((name, arch), False):
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


def _solve_requirements(oracle: Any, requirements: tuple[str, ...]) -> tuple[Any, set[str]]:
    result = oracle.resolve(requirements)
    missing = _solver_missing_caps(result)
    return result, missing


def _sat_job_requirements(requirements: tuple[str, ...] | list[str] | set[str]) -> tuple[str, ...]:
    from oreon_build.services.dep_plan.br_filter import is_vacuous_rich_requirement
    from oreon_build.services.dep_plan.providers import is_base_system_package
    from oreon_build.services.repo_capability_index import is_rpmlib_requirement

    out: list[str] = []
    seen: set[str] = set()
    for raw in requirements:
        cap = requirement_usable_for_solver(str(raw or ""))
        if not cap:
            continue
        if cap.lower().startswith("requested "):
            cap = cap[10:].strip()
        if not cap or cap in seen or is_spurious_solver_miss(cap):
            continue
        if is_rpmlib_requirement(cap) or is_vacuous_rich_requirement(cap):
            continue
        name, op, _ver = parse_versioned_requirement(cap)
        check = name or cap
        if not op and (is_base_system_package(check) or is_base_system_package(cap)):
            continue
        seen.add(cap)
        out.append(cap)
    return tuple(out)


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


def _spec_skips_layer_bootstrap(spec_text: str) -> bool:
    from oreon_build.services.dynamic_brs import spec_uses_cargo_generate_buildrequires

    return spec_uses_cargo_generate_buildrequires(spec_text or "")


async def _strip_bootstrap_stubs(db: Any, job: BuildJob) -> bool:
    opts = job_options_dict(job)
    if not opts.get("bootstrap_stub_provides") and not opts.get("bootstrap_dropped"):
        return False
    opts.pop("bootstrap_stub_provides", None)
    opts.pop("bootstrap_dropped", None)
    await _update_layer_job_if_active(
        db,
        job.id,
        job.layer_plan_id,
        job_options=opts,
    )
    job.job_options = opts
    return True


def _sat_extra_branches(
    publish_branch: str,
    releasename: str,
    mock_urls: list[str] | None,
) -> list[str]:
    from oreon_build.core.repo_branch import PRIMARY_REPO_BRANCHES

    chans = repo_channels_from_mock_urls(releasename, mock_urls)
    if not chans:
        chans = list(PRIMARY_REPO_BRANCHES)
    pub = (publish_branch or "").strip()
    return [c for c in chans if c != pub]


def _repo_oracle_for(
    releasename: str,
    branch: str,
    arch: str,
    extra_branches: list[str] | None = None,
) -> Any | None:
    extras = tuple(
        extra_branches
        if extra_branches is not None
        else default_capability_extra_branches(branch)
    )
    key = (releasename, branch, arch, extras)
    hit = _reactive_cache_get(_REACTIVE_SOLVER_CACHE, key)
    if hit is not None:
        return hit
    try:
        solver = get_repo_solver(
            releasename,
            branch,
            [arch],
            extra_branches=list(extras),
        )
    except Exception:
        logger.exception(
            "repo solver unavailable releasename=%s branch=%s arch=%s",
            releasename,
            branch,
            arch,
        )
        return None
    oracle = InstallOracle(solver)
    try:
        oracle.clear_overlay()
    except Exception:
        pass
    _reactive_cache_put(_REACTIVE_SOLVER_CACHE, key, oracle)
    return oracle


def _layer_blockers_for_caps(
    caps: set[str] | list[str] | tuple[str, ...],
    provider_map: dict[str, set[str]],
    package_names: set[str],
    layer_statuses: dict[tuple[str, str], set[BuildStatus]],
    architecture: str,
    consumer: str,
    layer_repo_ready: dict[tuple[str, str], bool] | None = None,
) -> set[str]:
    mapped: set[str] = set()
    for cap in caps:
        mapped.update(_layer_sources_for_requirement(str(cap), provider_map, package_names))
    return _open_layer_blockers(mapped, layer_statuses, architecture, consumer, layer_repo_ready)


def _open_layer_blockers(
    mapped: set[str],
    layer_statuses: dict[tuple[str, str], set[BuildStatus]],
    architecture: str,
    consumer: str,
    layer_repo_ready: dict[tuple[str, str], bool] | None = None,
) -> set[str]:
    arch = normalize_machine_arch(str(architecture or ""))
    ready = layer_repo_ready or {}
    names = set(mapped)
    names.discard(_canonical_layer_name(consumer))
    out: set[str] = set()
    for name in names:
        statuses = layer_statuses.get((name, arch), set())
        if not statuses:
            continue
        if ready.get((name, arch)):
            continue
        if all(item == BuildStatus.SKIPPED for item in statuses):
            continue
        out.add(name)
    return out


def _failed_upstream_sources(
    blockers: set[str],
    layer_statuses: dict[tuple[str, str], set[BuildStatus]],
    architecture: str,
) -> list[str]:
    arch = normalize_machine_arch(str(architecture or ""))
    failed: list[str] = []
    for source in sorted(blockers):
        statuses = layer_statuses.get((source, arch), set())
        if BuildStatus.FAILED in statuses or BuildStatus.CANCELED in statuses:
            failed.append(source)
    return failed


async def _settle_layer_fixpoint(db: Any, plan: LayerPlan) -> None:
    await db.refresh(plan)
    if plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
        return
    rows = await db.execute(
        select(BuildJob.status, BuildJob.job_options).where(BuildJob.layer_plan_id == plan.id)
    )
    active_count = 0
    failed_count = 0
    blocked_count = 0
    unpublished_success = 0
    for job_status, job_opts in rows.all():
        if job_status in (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING):
            active_count += 1
        elif job_status == BuildStatus.FAILED:
            failed_count += 1
        elif job_status == BuildStatus.BLOCKED:
            blocked_count += 1
        elif job_status == BuildStatus.SUCCESS and not _job_repo_published(
            job_status, job_opts if isinstance(job_opts, dict) else None
        ):
            unpublished_success += 1
    if active_count > 0 or unpublished_success > 0:
        return
    if failed_count <= 0 or blocked_count > 0:
        return
    plan.status = LayerPlanStatus.FAILED
    plan.error_message = (
        "Layer failed due to upstream package failures "
        f"({failed_count} failed, {blocked_count} blocked)."
    )


async def _evaluate_layer_buildability(
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
    task = asyncio.current_task()
    tid = id(task) if task is not None else 0
    nested = _REACTIVE_PLAN_LOCK_OWNERS.get(plan_id) == tid and lock.locked()
    if not nested:
        await asyncio.sleep(0)
        await lock.acquire()
        _REACTIVE_PLAN_LOCK_OWNERS[plan_id] = tid
    try:
        changed: list[BuildJob] = []
        while True:
            batch, expanded = await _evaluate_layer_buildability_locked(
                db,
                plan_id,
                source_name=source_name,
                published_architecture=published_architecture,
            )
            changed.extend(batch)
            if not expanded:
                break
            source_name = None
            published_architecture = None
        plan = await db.get(LayerPlan, plan_id)
        if plan is not None:
            await _settle_layer_fixpoint(db, plan)
        if not nested:
            from oreon_build.services.layer_plan import maybe_settle_layer_plan

            await maybe_settle_layer_plan(db, plan_id)
        return changed
    finally:
        if not nested:
            _REACTIVE_PLAN_LOCK_OWNERS.pop(plan_id, None)
            lock.release()


async def _evaluate_layer_buildability_locked(
    db: Any,
    layer_plan_id: int,
    source_name: str | None = None,
    published_architecture: str | None = None,
) -> tuple[list[BuildJob], bool]:
    plan = await db.get(LayerPlan, int(layer_plan_id))
    if plan is None:
        return [], False
    await db.refresh(plan)
    if plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
        return [], False
    release = await db.get(Release, plan.release_id)
    if release is None:
        return [], False
    side_tag = await db.get(SideTag, plan.side_tag_id) if plan.side_tag_id else None
    if side_tag is None:
        return [], False
    options = _plan_options(plan)
    releasename = (release.releasename or "").strip()
    branch = (side_tag.name or "").strip()
    distgit_url = str(options.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    owner_repo = _github_owner_repo_from_git_url(distgit_url)
    if not owner_repo:
        return [], False
    owner, repo = owner_repo
    distgit_branch = str(options.get("distgit_branch") or release_distgit_branch(release)).strip()
    architectures = [str(a) for a in (options.get("architectures") or DEFAULT_ARCH) if a] or list(DEFAULT_ARCH)
    mock_urls: list[str] = []
    mock_id = options.get("preferred_mock_environment_id") or getattr(
        release, "default_mock_environment_id", None
    )
    if mock_id:
        mock_env = await db.get(MockEnvironment, int(mock_id))
        if mock_env is not None:
            mock_urls = [
                str(u).strip()
                for u in (mock_env.additional_repo_urls or [])
                if str(u).strip()
            ]
    sat_extra = _sat_extra_branches(branch, releasename, mock_urls)
    if source_name or published_architecture:
        logger.info(
            "layer eval trigger plan=%s source=%s arch=%s",
            plan.id,
            source_name,
            published_architecture,
        )
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
    layer_statuses: dict[tuple[str, str], set[BuildStatus]] = {}
    layer_package_names: set[str] = set()
    layer_repo_ready: dict[tuple[str, str], bool] = {}
    for name, job_status, job_arch, job_opts in layer_rows_result.all():
        if not name:
            continue
        canonical_name = _canonical_layer_name(str(name))
        layer_package_names.add(canonical_name)
        arch_key = normalize_machine_arch(str(job_arch or ""))
        layer_statuses.setdefault((canonical_name, arch_key), set()).add(job_status)
        opts = job_opts if isinstance(job_opts, dict) else {}
        if job_status == BuildStatus.SKIPPED:
            published = True
        elif job_status == BuildStatus.SUCCESS:
            published = _job_repo_published(job_status, opts)
        else:
            published = False
        prev = layer_repo_ready.get((canonical_name, arch_key))
        layer_repo_ready[(canonical_name, arch_key)] = published if prev is None else (prev or published)
    all_pkg_ids = {int(job.package_id) for job, _architecture in row_list}
    packages_by_id: dict[int, Package] = {}
    if all_pkg_ids:
        pkg_rows = await db.execute(select(Package).where(Package.id.in_(all_pkg_ids)))
        packages_by_id = {int(pkg.id): pkg for pkg in pkg_rows.scalars().all()}
    work_list = list(row_list)
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
    cargo_cleared = False
    for job, _architecture in work_list:
        if await _strip_bootstrap_stubs(db, job):
            cargo_cleared = True
    if cargo_cleared:
        await db.flush()
        await db.commit()
    prepared: dict[tuple[int, str], tuple[Package, str | None, Any | None, tuple[str, ...], bool]] = {}
    prepare_gate = asyncio.Semaphore(8)

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
            cached_req = _reactive_cache_get(_REACTIVE_REQUIREMENTS_CACHE, req_key)
            if cached_req is not None:
                requirements, complete = cached_req
                prepared[(int(job.id), arch)] = (
                    package, text, _SPEC_UNPARSED, tuple(requirements), complete,
                )
                return
            profile_defines = (
                dict(opts_probe.get("rpmbuild_defines") or {}) if profile_is_bootstrap else {}
            )
            try:
                parsed, requirements, complete = await _layer_to_thread(
                    _prepare_spec_requirements,
                    text,
                    package.name,
                    releasename,
                    arch,
                    profile_defines or None,
                )
            except Exception:
                logger.exception("spec parse failed package=%s arch=%s", package.name, arch)
                prepared[(int(job.id), arch)] = (package, text, None, (), False)
                return
            _reactive_cache_put(_REACTIVE_REQUIREMENTS_CACHE, req_key, (requirements, complete))
            prepared[(int(job.id), arch)] = (package, text, parsed, tuple(requirements), complete)

    await asyncio.gather(*(prepare_job(job, architecture) for job, architecture in work_list))
    await db.refresh(plan)
    await asyncio.sleep(0)
    writes = 0
    sat_n = 0
    expanded = False

    async def _checkpoint() -> None:
        nonlocal writes
        writes += 1
        await db.flush()
        await db.commit()
        await db.refresh(plan)
        await asyncio.sleep(0)

    for job, architecture in work_list:
        if plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
            logger.info("layer eval stop plan=%s status=%s", plan.id, plan.status)
            break
        await asyncio.sleep(0)
        arch = normalize_machine_arch(str(architecture or ""))
        prepared_job = prepared.get((int(job.id), arch))
        if prepared_job is None:
            continue
        package, text, parsed, requirements, complete = prepared_job
        consumer = _canonical_layer_name(package.name)
        hold_caps = _sat_hold_caps_from_job(job)
        effective_requirements = _merge_requirement_caps(requirements, hold_caps)
        if not text:
            await _update_layer_job_if_active(
                db, job.id, plan.id, status=BuildStatus.BLOCKED,
                blocked_reason="waiting on repository: source unavailable", blocking_sources=[],
            )
            await _checkpoint()
            continue
        if parsed is None:
            await _update_layer_job_if_active(
                db, job.id, plan.id, status=BuildStatus.BLOCKED,
                blocked_reason="waiting on repository: spec parse failed", blocking_sources=[],
            )
            await _checkpoint()
            continue
        if parsed is _SPEC_UNPARSED:
            parsed = None
        oracle = await asyncio.to_thread(_repo_oracle_for, releasename, branch, arch, sat_extra)
        sat_reqs = _sat_job_requirements(effective_requirements)
        missing: set[str] = set()
        real_result: Any = None
        if oracle is None:
            await _update_layer_job_if_active(
                db, job.id, plan.id, status=BuildStatus.BLOCKED,
                blocked_reason="waiting on repository: solver unavailable",
                blocking_sources=[],
            )
            await _checkpoint()
            continue
        if sat_reqs:
            sat_n += 1
            if sat_n == 1 or sat_n % 10 == 0:
                logger.info(
                    "layer sat plan=%s job=%s pkg=%s reqs=%s",
                    plan.id,
                    job.id,
                    package.name,
                    len(sat_reqs),
                )
            real_result, missing = await asyncio.to_thread(
                _solve_requirements, oracle, sat_reqs
            )
            sat_ok = bool(getattr(real_result, "ok", False)) and not missing
        else:
            sat_ok = True
        if missing:
            miss_blockers = _layer_blockers_for_caps(
                missing, provider_map, layer_package_names, layer_statuses, arch, consumer,
                layer_repo_ready,
            )
            failed_sources = _failed_upstream_sources(miss_blockers, layer_statuses, arch)
            if failed_sources:
                reason = "upstream failure: " + ", ".join(failed_sources)
                await _update_layer_job_if_active(
                    db,
                    job.id,
                    plan.id,
                    status=BuildStatus.BLOCKED,
                    blocked_reason=reason,
                    blocking_sources=sorted(miss_blockers),
                )
                job.status = BuildStatus.BLOCKED
                job.blocked_reason = reason
                job.blocking_sources = sorted(miss_blockers)
                await _checkpoint()
                continue
            if miss_blockers:
                await _update_layer_job_if_active(
                    db,
                    job.id,
                    plan.id,
                    status=BuildStatus.BLOCKED,
                    blocked_reason="waiting on layer jobs: " + ", ".join(sorted(miss_blockers)),
                    blocking_sources=sorted(miss_blockers),
                )
                await _checkpoint()
                continue
            added, heal_blockers = await _expand_missing_provider(
                db, int(plan.id), set(missing), plan=plan, release=release,
                side_tag_name=branch, architectures=architectures, options=options,
                distgit_url=distgit_url, distgit_branch=distgit_branch, owner=owner, repo=repo,
                releasename=releasename, provider_index=provider_index,
                headers=_github_headers(),
                consumer=consumer,
            )
            if added:
                expanded = True
            if heal_blockers:
                await _update_layer_job_if_active(
                    db,
                    job.id,
                    plan.id,
                    status=BuildStatus.BLOCKED,
                    blocked_reason="waiting on layer jobs: " + ", ".join(sorted(heal_blockers)),
                    blocking_sources=sorted(heal_blockers),
                )
                await _checkpoint()
                continue
        if not sat_ok:
            await _update_layer_job_if_active(
                db,
                job.id,
                plan.id,
                status=BuildStatus.BLOCKED,
                blocked_reason="waiting on repository: " + ", ".join(sorted(missing))
                if missing
                else "waiting on repository: solver conflict",
                blocking_sources=[],
            )
            await _checkpoint()
            continue
        if not complete:
            await _update_layer_job_if_active(
                db,
                job.id,
                plan.id,
                status=BuildStatus.BLOCKED,
                blocked_reason="waiting on repository: generate_buildrequires incomplete",
                blocking_sources=[],
            )
            await _checkpoint()
            continue
        if job.status != BuildStatus.QUEUED:
            if hold_caps:
                opts = job_options_dict(job)
                opts.pop("sat_hold_caps", None)
                await _update_layer_job_if_active(
                    db, job.id, plan.id, job_options=opts,
                )
                job.job_options = opts
            changed_now = await _update_layer_job_if_active(
                db, job.id, plan.id, status=BuildStatus.QUEUED, blocked_reason="", blocking_sources=[]
            )
            if changed_now:
                job.status = BuildStatus.QUEUED
                job.blocked_reason = ""
                job.blocking_sources = []
                changed.append(job)
        else:
            await _update_layer_job_if_active(db, job.id, plan.id, blocked_reason="", blocking_sources=[])
        await _checkpoint()
    await db.flush()
    if plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
        logger.info("layer eval abort plan=%s status=%s queued=%s", plan.id, plan.status, len(changed))
        return changed, False
    logger.info(
        "layer eval pass plan=%s queued=%s expanded=%s",
        plan.id,
        len(changed),
        expanded,
    )
    return changed, expanded


async def evaluate_layer_buildability(
    db: Any,
    layer_plan_id: int,
    source_name: str | None = None,
    published_architecture: str | None = None,
) -> list[BuildJob]:
    plan_id = int(layer_plan_id)
    plan = await db.get(LayerPlan, plan_id)
    if plan is not None and plan.status == LayerPlanStatus.FAILED:
        stall = (plan.error_message or "").strip().lower()
        if (
            stall.startswith("layer stalled")
            or "unresolvable repository deadlock" in stall
            or "upstream package failures" in stall
        ):
            plan.status = LayerPlanStatus.RUNNING
            plan.error_message = None
            await db.flush()
    changed = await _evaluate_layer_buildability(
        db,
        plan_id,
        source_name=source_name,
        published_architecture=published_architecture,
    )
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
                        published_job.status = BuildStatus.BLOCKED
                        published_job.blocked_reason = "waiting on repository"
                        published_job.blocking_sources = []
            if plan is not None:
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
                LayerPlan.error_message.ilike("%blocked%"),
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
