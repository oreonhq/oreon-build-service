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
import logging
import re
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.core.repo_branch import release_distgit_branch
from oreon_build.models import (
    BuildAttempt,
    BuildJob,
    BuildStatus,
    BuildTarget,
    LayerJobBlocker,
    LayerPlan,
    LayerPlanStatus,
    MockEnvironment,
    Package,
    Release,
    Worker,
)
from oreon_build.services.build_pass import apply_plan_to_job_options
from oreon_build.services.build_target_util import get_or_create_build_target, resolve_mock_environment_id
from oreon_build.services.dep_plan.install_oracle import InstallOracle, overlay_from_nodes
from oreon_build.services.dep_plan.order import topo_layers
from oreon_build.services.dep_plan.providers import providers_for_requirement
from oreon_build.services.distgit_order import (
    _fetch_distgit_specs_batch,
    _github_owner_repo_from_git_url,
    _spec_parse_fn,
    dependency_capabilities,
    distgit_providers_dict,
    expand_chain_spec_closure_sparse,
    get_distgit_provides_index_cached,
)
from oreon_build.services.layer_plan import get_or_create_package
from oreon_build.services.repo_capability_index import (
    RepoCapabilityIndex,
    clear_repo_capability_cache,
    default_capability_extra_branches,
    load_repo_capability_index,
)
from oreon_build.services.repo_solver import get_repo_solver

if TYPE_CHECKING:
    from oreon_build.models import Account

logger = logging.getLogger(__name__)

DEFAULT_ARCH = ["x86_64", "aarch64"]

_RE_NOTHING_PROVIDES_REQUESTED = re.compile(
    r"nothing provides requested\s+(?P<cap>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RE_NOTHING_PROVIDES = re.compile(
    r"nothing provides\s+(?P<cap>.+?)(?:\s+needed by\s|\s*$)",
    re.IGNORECASE,
)


def parse_missing_provides_from_text(text: str) -> set[str]:
    out: set[str] = set()
    raw = text or ""
    for cre in (_RE_NOTHING_PROVIDES_REQUESTED, _RE_NOTHING_PROVIDES):
        for m in cre.finditer(raw):
            cap = (m.group("cap") or "").strip()
            if cap and "%" not in cap:
                out.add(cap)
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


async def _misses_from_job_build_log(db: Any, job: BuildJob) -> set[str]:
    """Worker used to head-truncate error_message. Pull misses from the R2 log."""
    key = await db.scalar(
        select(BuildAttempt.log_r2_key)
        .where(BuildAttempt.build_job_id == job.id, BuildAttempt.log_r2_key.isnot(None))
        .order_by(BuildAttempt.id.desc())
        .limit(1)
    )
    if not key:
        return set()
    try:
        from oreon_build.services.r2 import get_r2_client

        raw = await asyncio.to_thread(get_r2_client().get_object, key)
    except Exception:
        logger.exception("autofix log fetch failed job_id=%s key=%s", job.id, key)
        return set()
    if not raw:
        return set()
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if len(text) > 256_000:
        text = text[-256_000:]
    return parse_missing_provides_from_text(text)


def _plan_options(plan: LayerPlan) -> dict[str, Any]:
    raw = plan.plan_options
    return dict(raw) if isinstance(raw, dict) else {}


async def _create_job_for_package(
    db: Any,
    plan: LayerPlan,
    pkg_name: str,
    *,
    release: Release,
    workers_by_arch: dict[str, bool],
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
    )
    pref_mock = resolve_mock_environment_id(
        trigger_mock_environment_id=preferred_mock_environment_id,
        package_mock_environment_id=None,
        release_default_mock_environment_id=release.default_mock_environment_id,
    )
    jobs: list[BuildJob] = []
    for arch in architectures:
        canon = normalize_machine_arch(arch)
        if not workers_by_arch.get(canon, False):
            continue
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
        await db.flush()
        jobs.append(job)
        if triggered_by_id is not None:
            await log_audit(
                db,
                "build.trigger",
                account_id=triggered_by_id,
                resource_type="build_job",
                resource_id=str(job.id),
                details=f"layer_plan_id={plan.id} layer_index={layer_index} package={pkg_name}",
                ip_address=get_client_ip(request) if request is not None else None,
                user_agent=get_user_agent(request) if request is not None else "layer-build",
            )
    return jobs


def _provider_sources_for_miss(
    miss: str,
    *,
    pkg: str,
    provider_map: dict[str, set[str]],
    package_names: set[str],
    cap_index: RepoCapabilityIndex,
) -> set[str]:
    srcs = providers_for_requirement(
        miss, provider_map=provider_map, repo_index=cap_index, package_names=package_names
    )
    return {s for s in srcs if s and s != pkg}


def _miss_is_self_provided(
    miss: str,
    *,
    pkg: str,
    provider_map: dict[str, set[str]],
    package_names: set[str],
    cap_index: RepoCapabilityIndex,
) -> bool:
    srcs = providers_for_requirement(
        miss, provider_map=provider_map, repo_index=cap_index, package_names=package_names
    )
    return bool(srcs) and all(s == pkg for s in srcs)


def _parsed_evr(parsed: Any) -> tuple[int, str, str]:
    try:
        epoch = int(getattr(parsed, "epoch", 0) or 0)
    except (TypeError, ValueError):
        epoch = 0
    return epoch, str(getattr(parsed, "version", "") or ""), str(getattr(parsed, "release", "") or "")


def _source_needs_build(
    name: str,
    *,
    cap_index: RepoCapabilityIndex,
    parsed_by_source: dict[str, Any] | None = None,
    always_build: set[str] | None = None,
    epoch: int = 0,
    version: str = "",
    release: str = "",
    require_evr: bool = False,
) -> bool:
    """False when we should prefer the repo copy of this source.

    require_evr=True for unmet-capability providers: only skip when distgit EVR is
    known and repo is already >= that EVR. Presence alone is not enough (arch holes).
    """
    n = (name or "").strip()
    if not n:
        return False
    if always_build and n in always_build:
        return True
    parsed = (parsed_by_source or {}).get(n)
    if parsed is not None:
        epoch, version, release = _parsed_evr(parsed)
    if cap_index.source_is_repo_current(
        n, epoch, version, release, require_evr=require_evr
    ):
        return False
    return True


def _unresolved_hint(miss: str) -> str:
    from oreon_build.services.virtual_resolve import import_suggestion_for_cap

    raw = (miss or "").strip().rstrip("\\").strip("{}").strip()
    if not raw or (len(raw) == 1 and not raw.isalnum()):
        return ""
    return import_suggestion_for_cap(raw) or raw


def _add_leaf_br_edges(
    deps: dict[str, set[str]],
    pkg: str,
    parsed: Any,
    *,
    provider_map: dict[str, set[str]],
    package_names: set[str],
    cap_index: RepoCapabilityIndex,
    parsed_by_source: dict[str, Any] | None = None,
    always_build: set[str] | None = None,
) -> None:
    for req in dependency_capabilities(parsed):
        if cap_index.satisfies_requirement(req):
            continue
        if _miss_is_self_provided(
            req, pkg=pkg, provider_map=provider_map, package_names=package_names, cap_index=cap_index
        ):
            continue
        srcs = _provider_sources_for_miss(
            req, pkg=pkg, provider_map=provider_map, package_names=package_names, cap_index=cap_index
        )
        for s in srcs:
            if not _source_needs_build(
                s,
                cap_index=cap_index,
                parsed_by_source=parsed_by_source,
                always_build=always_build,
                require_evr=True,
            ):
                continue
            deps.setdefault(pkg, set()).add(s)


def _overlay_nodes_for_parsed(
    parsed_by_source: dict[str, Any],
    *,
    only: set[str] | None = None,
) -> dict[str, Any]:
    from types import SimpleNamespace

    nodes: dict[str, Any] = {}
    for name, parsed in parsed_by_source.items():
        if only is not None and name not in only:
            continue
        bins = set(getattr(parsed, "provided_binary_names", None) or ())
        bins.add(name)
        epoch, version, release = _parsed_evr(parsed)
        nodes[name] = SimpleNamespace(
            action="pending",
            provides=bins,
            version=version or "0",
            release=release or "0",
            epoch=epoch,
            binaries=tuple(bins),
        )
    return nodes


def _filter_build_set(
    roots: list[str],
    deps: dict[str, set[str]],
    parsed_by_source: dict[str, Any],
    unresolved: dict[str, set[str]],
    cap_index: RepoCapabilityIndex,
) -> tuple[dict[str, set[str]], set[str], dict[str, set[str]]]:
    """Keep roots + missing/outdated providers only. Drop repo-current transitive pkgs."""
    always = {(r or "").strip() for r in roots if (r or "").strip()}
    # roots only count once we actually parsed a distgit spec for them
    build_set: set[str] = {n for n in always if n in parsed_by_source}
    cleaned: dict[str, set[str]] = {n: set() for n in parsed_by_source}
    for pkg, blockers in deps.items():
        for b in blockers:
            if not _source_needs_build(
                b,
                cap_index=cap_index,
                parsed_by_source=parsed_by_source,
                always_build=always,
                require_evr=True,
            ):
                continue
            cleaned.setdefault(pkg, set()).add(b)

    changed = True
    while changed:
        changed = False
        for pkg in list(build_set):
            for b in cleaned.get(pkg, ()):
                if b in build_set:
                    continue
                if b not in parsed_by_source:
                    continue
                if not _source_needs_build(
                    b,
                    cap_index=cap_index,
                    parsed_by_source=parsed_by_source,
                    always_build=always,
                    require_evr=True,
                ):
                    continue
                build_set.add(b)
                changed = True

    out_deps = {n: set(cleaned.get(n, ())) & build_set for n in build_set}
    out_unres = {n: set(unresolved.get(n) or ()) for n in build_set}
    return out_deps, build_set, out_unres


async def _static_close_dependencies(
    roots: list[str],
    *,
    releasename: str,
    owner: str,
    repo: str,
    distgit_branch: str,
    headers: dict[str, str],
    target_arches: set[str] | None,
    cap_index: RepoCapabilityIndex,
    publish_branch: str,
    architectures: list[str],
) -> tuple[dict[str, set[str]], set[str], list[str], dict[str, set[str]]]:
    """Static walk + libsolv installability: consumer -> unbuilt provider sources.

    Leaf BR holes are not enough (go-rpm-macros can be present while golist is
    missing). Solv installability of the BR set finds those transitive holes and
    pulls their distgit sources into the plan as blockers.
    """
    from oreon_build.core.rpm_requires import merge_provider_maps

    always_build = {(r or "").strip() for r in roots if (r or "").strip()}
    provides_index = await get_distgit_provides_index_cached(
        owner=owner, repo=repo, distgit_branch=distgit_branch, releasename=releasename, headers=headers
    )
    provider_map: dict[str, set[str]] = {}
    package_names: set[str] = set()
    if provides_index is not None:
        provider_map = merge_provider_maps(distgit_providers_dict(provides_index))
        package_names = set(provides_index.package_names)

    specs_by_pkg, parsed_by_source, errors = await expand_chain_spec_closure_sparse(
        roots,
        releasename=releasename,
        owner=owner,
        repo=repo,
        distgit_branch=distgit_branch,
        headers=headers,
        repo_index=cap_index,
        target_arches=target_arches,
        distgit_providers=provider_map or None,
        always_build=always_build,
    )

    deps: dict[str, set[str]] = {n: set() for n in parsed_by_source}
    unresolved: dict[str, set[str]] = {n: set() for n in parsed_by_source}
    for pkg, parsed in parsed_by_source.items():
        if pkg not in always_build and not _source_needs_build(
            pkg, cap_index=cap_index, parsed_by_source=parsed_by_source, always_build=always_build
        ):
            continue
        _add_leaf_br_edges(
            deps,
            pkg,
            parsed,
            provider_map=provider_map,
            package_names=package_names,
            cap_index=cap_index,
            parsed_by_source=parsed_by_source,
            always_build=always_build,
        )

    arches = [str(a) for a in architectures if a] or list(DEFAULT_ARCH)
    branch = (publish_branch or "").strip() or "dev"
    solver = await asyncio.to_thread(
        get_repo_solver,
        releasename,
        branch,
        arches,
        extra_branches=default_capability_extra_branches(branch),
    )
    install = InstallOracle(solver)
    try:
        install_done: set[str] = set()

        def _overlay_names() -> set[str]:
            return {
                n
                for n in parsed_by_source
                if _source_needs_build(
                    n,
                    cap_index=cap_index,
                    parsed_by_source=parsed_by_source,
                    always_build=always_build,
                )
            }

        def _ready_pkgs() -> list[str]:
            return sorted(
                n
                for n in _overlay_names()
                if not deps.get(n) and not unresolved.get(n)
            )

        def _fold_misses(pkg: str, misses: set[str], to_fetch: set[str]) -> set[str]:
            pkg_unres: set[str] = set()
            for miss in sorted(misses):
                if cap_index.satisfies_requirement(miss):
                    continue
                if _miss_is_self_provided(
                    miss,
                    pkg=pkg,
                    provider_map=provider_map,
                    package_names=package_names,
                    cap_index=cap_index,
                ):
                    continue
                srcs = _provider_sources_for_miss(
                    miss,
                    pkg=pkg,
                    provider_map=provider_map,
                    package_names=package_names,
                    cap_index=cap_index,
                )
                kept: set[str] = set()
                for s in srcs:
                    if not _source_needs_build(
                        s,
                        cap_index=cap_index,
                        parsed_by_source=parsed_by_source,
                        always_build=always_build,
                        require_evr=True,
                    ):
                        continue
                    kept.add(s)
                if not kept:
                    hint = _unresolved_hint(miss)
                    if hint:
                        pkg_unres.add(hint)
                    continue
                deps.setdefault(pkg, set()).update(kept)
                for s in kept:
                    if s not in parsed_by_source:
                        to_fetch.add(s)
            return pkg_unres

        for _round in range(64):
            ready = _ready_pkgs()
            if not ready:
                break
            install.set_overlay(
                overlay_from_nodes(
                    _overlay_nodes_for_parsed(parsed_by_source, only=_overlay_names()),
                    provider_map,
                )
            )
            to_fetch: set[str] = set()
            progressed = False

            for pkg in ready:
                if pkg in install_done:
                    continue
                install_done.add(pkg)
                progressed = True
                parsed = parsed_by_source[pkg]
                brs = [c for c in dependency_capabilities(parsed) if c and "%" not in c]
                if not brs:
                    unresolved.get(pkg, set()).clear()
                    continue
                result = install.resolve(brs)
                if result.ok:
                    unresolved.get(pkg, set()).clear()
                    continue
                unresolved[pkg] = _fold_misses(pkg, set(result.missing_provides or ()), to_fetch)

            if to_fetch:
                grew = await _merge_fetched_sources(
                    to_fetch,
                    specs_by_pkg=specs_by_pkg,
                    parsed_by_source=parsed_by_source,
                    deps=deps,
                    unresolved=unresolved,
                    errors=errors,
                    releasename=releasename,
                    owner=owner,
                    repo=repo,
                    distgit_branch=distgit_branch,
                    headers=headers,
                    cap_index=cap_index,
                    target_arches=target_arches,
                    provider_map=provider_map,
                    package_names=package_names,
                    always_build=always_build,
                )
                if not grew and not progressed:
                    break
                continue
            if not progressed:
                break
    finally:
        try:
            install.clear_overlay()
        except Exception:
            pass

    deps, build_set, unresolved = _filter_build_set(
        roots, deps, parsed_by_source, unresolved, cap_index
    )
    return deps, build_set, errors, unresolved


async def _merge_fetched_sources(
    to_fetch: set[str],
    *,
    specs_by_pkg: dict[str, str],
    parsed_by_source: dict[str, Any],
    deps: dict[str, set[str]],
    unresolved: dict[str, set[str]],
    errors: list[str],
    releasename: str,
    owner: str,
    repo: str,
    distgit_branch: str,
    headers: dict[str, str],
    cap_index: RepoCapabilityIndex,
    target_arches: set[str] | None,
    provider_map: dict[str, set[str]],
    package_names: set[str],
    always_build: set[str] | None = None,
) -> bool:
    force = set(always_build or ())
    more_specs, more_parsed, more_err = await expand_chain_spec_closure_sparse(
        sorted(to_fetch),
        releasename=releasename,
        owner=owner,
        repo=repo,
        distgit_branch=distgit_branch,
        headers=headers,
        repo_index=cap_index,
        target_arches=target_arches,
        distgit_providers=provider_map or None,
        always_build=force,
    )
    errors.extend(more_err or [])
    if more_specs:
        specs_by_pkg.update(more_specs)
    grew = False
    for name, parsed in (more_parsed or {}).items():
        if name in parsed_by_source:
            continue
        parsed_by_source[name] = parsed
        if not _source_needs_build(
            name,
            cap_index=cap_index,
            parsed_by_source=parsed_by_source,
            always_build=force,
            require_evr=True,
        ):
            # prefer repo: drop edges that pointed here, do not fan out
            for consumer, blockers in list(deps.items()):
                blockers.discard(name)
            continue
        grew = True
        deps.setdefault(name, set())
        unresolved.setdefault(name, set())
        _add_leaf_br_edges(
            deps,
            name,
            parsed,
            provider_map=provider_map,
            package_names=package_names,
            cap_index=cap_index,
            parsed_by_source=parsed_by_source,
            always_build=force,
        )
    return grew


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
    workers_result = await db.execute(select(Worker))
    workers_by_arch: dict[str, bool] = {}
    for w in workers_result.scalars().all():
        workers_by_arch[normalize_machine_arch(w.architecture)] = True
    if not any(workers_by_arch.get(normalize_machine_arch(a), False) for a in arches):
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


async def complete_seed_layer_plan(
    db: Any,
    plan_id: int,
    *,
    headers: dict[str, str] | None = None,
) -> list[BuildJob]:
    """Background half: static dep walk + job creation for a PENDING LayerPlan."""
    plan = await db.get(LayerPlan, plan_id, with_for_update=True)
    if plan is None:
        return []
    if plan.status != LayerPlanStatus.PENDING:
        return []
    job_n = await db.scalar(select(BuildJob.id).where(BuildJob.layer_plan_id == plan.id).limit(1))
    if job_n is not None:
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

    opt = _plan_options(plan)
    arches = [str(a) for a in (opt.get("architectures") or DEFAULT_ARCH) if a] or list(DEFAULT_ARCH)
    dg_url = str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    dg_branch = str(opt.get("distgit_branch") or release_distgit_branch(release)).strip()
    preferred_mock_environment_id = opt.get("preferred_mock_environment_id")
    priority = int(opt.get("priority") or 0)
    roots = list(plan.root_packages or [])
    releasename = (release.releasename or "").strip()
    side_tag_name = side_tag.name
    triggered_by_id = plan.triggered_by_id
    gr = _github_owner_repo_from_git_url(dg_url)
    if not gr:
        await fail_seed(db, plan, f"distgit_url is not a supported github.com URL: {dg_url!r}")
        return []
    owner, distgit_repo = gr
    target_arches = {a.lower() for a in arches if a} or None

    workers_result = await db.execute(select(Worker))
    workers_by_arch: dict[str, bool] = {}
    for w in workers_result.scalars().all():
        workers_by_arch[normalize_machine_arch(w.architecture)] = True

    # drop the FOR UPDATE txn before long distgit/solv work or idle_in_transaction
    # will kill the connection mid-seed
    await db.commit()

    try:
        cap_index = await asyncio.to_thread(
            load_repo_capability_index,
            releasename,
            side_tag_name,
            arches,
            extra_branches=default_capability_extra_branches(side_tag_name),
        )
        deps, known, errors, unresolved = await _static_close_dependencies(
            roots,
            releasename=releasename,
            owner=owner,
            repo=distgit_repo,
            distgit_branch=dg_branch,
            headers=headers or _github_headers(),
            target_arches=target_arches,
            cap_index=cap_index,
            publish_branch=side_tag_name,
            architectures=arches,
        )
    except Exception as exc:
        logger.exception("layer_plan_id=%s seeding failed", plan_id)
        plan = await db.get(LayerPlan, plan_id, with_for_update=True)
        if plan is not None and plan.status == LayerPlanStatus.PENDING:
            await fail_seed(db, plan, f"Layer seeding failed: {exc}")
        return []

    missing_roots = [r for r in roots if r not in known]
    if missing_roots:
        plan = await db.get(LayerPlan, plan_id, with_for_update=True)
        if plan is not None and plan.status == LayerPlanStatus.PENDING:
            detail = f"Package(s) not found in distgit: {', '.join(missing_roots)}"
            if errors:
                detail = f"{detail}; {errors[0]}"
            await fail_seed(db, plan, detail)
        return []

    plan = await db.get(LayerPlan, plan_id, with_for_update=True)
    if plan is None or plan.status != LayerPlanStatus.PENDING:
        return []
    job_n = await db.scalar(select(BuildJob.id).where(BuildJob.layer_plan_id == plan.id).limit(1))
    if job_n is not None:
        return []
    release = await db.get(Release, plan.release_id)
    if release is None:
        await fail_seed(db, plan, "Release not found")
        return []

    all_names = set(known)
    deps = break_dep_cycles(
        deps,
        repo_current=lambda n: bool(
            cap_index.source_present_in_repo(n) or n in cap_index.binary_names
        ),
    )
    layer_names, _cycle = topo_layers(all_names, deps)
    layer_index_by_name = {n: i for i, layer in enumerate(layer_names) for n in layer}

    jobs: list[BuildJob] = []
    job_id_by_pkg: dict[str, list[BuildJob]] = {}
    for layer in layer_names:
        for pkg_name in layer:
            blockers = sorted(deps.get(pkg_name, set()))
            gaps = sorted(unresolved.get(pkg_name) or ())
            if gaps and not blockers:
                initial_status = BuildStatus.BLOCKED
                reason = f"cannot resolve: {', '.join(gaps[:8])} (import into distgit)"
            else:
                initial_status, reason = initial_job_status(set(blockers))
                if gaps and reason:
                    reason = f"{reason}; cannot resolve: {', '.join(gaps[:4])}"
            pkg_jobs = await _create_job_for_package(
                db,
                plan,
                pkg_name,
                release=release,
                workers_by_arch=workers_by_arch,
                architectures=arches,
                distgit_url=dg_url,
                distgit_branch=dg_branch,
                preferred_mock_environment_id=preferred_mock_environment_id,
                priority=priority,
                triggered_by_id=triggered_by_id,
                request=None,
                releasename=releasename,
                publish_branch=side_tag_name,
                layer_index=layer_index_by_name.get(pkg_name, 0),
                initial_status=initial_status,
                blocked_reason=reason,
            )
            job_id_by_pkg[pkg_name] = pkg_jobs
            jobs.extend(pkg_jobs)
            for job in pkg_jobs:
                for blk in blockers:
                    db.add(LayerJobBlocker(build_job_id=job.id, blocking_source=blk))
            await db.flush()

    if not jobs:
        await fail_seed(db, plan, "No jobs queued (no workers for selected arches).")
        return []

    plan.status = LayerPlanStatus.RUNNING
    await db.flush()
    logger.info(
        "layer_plan_id=%s seeded roots=%s packages=%s queued=%s blocked=%s errors=%s",
        plan.id,
        len(roots),
        len(job_id_by_pkg),
        sum(1 for j in jobs if j.status == BuildStatus.QUEUED),
        sum(1 for j in jobs if j.status == BuildStatus.BLOCKED),
        len(errors),
    )
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
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), headers=headers) as client:
        batch = await _fetch_distgit_specs_batch(
            client, owner=owner, repo=repo, distgit_branch=distgit_branch, releasename=releasename, packages=[pkg_name]
        )
    return batch.get(pkg_name)


def _resolve_install_holes(
    requirements: list[str],
    *,
    pkg_name: str,
    cap_index: RepoCapabilityIndex,
    releasename: str,
    publish_branch: str,
    architectures: list[str],
    provider_map: dict[str, set[str]] | None = None,
    package_names: set[str] | None = None,
) -> tuple[set[str], set[str]]:
    """Return (provider_sources, unresolved_caps) from libsolv on requirements."""
    brs = [c for c in requirements if c and "%" not in c]
    if not brs:
        return set(), set()
    pmap = provider_map or {}
    known = package_names or set()
    branch = (publish_branch or "").strip() or "dev"
    arches = [str(a) for a in architectures if a] or list(DEFAULT_ARCH)
    solver = get_repo_solver(
        releasename,
        branch,
        arches,
        extra_branches=default_capability_extra_branches(branch),
    )
    install = InstallOracle(solver)
    try:
        result = install.resolve(brs)
    finally:
        try:
            install.clear_overlay()
        except Exception:
            pass
    if result.ok:
        return set(), set()
    sources: set[str] = set()
    unresolved: set[str] = set()
    for miss in result.missing_provides or ():
        srcs = _provider_sources_for_miss(
            miss, pkg=pkg_name, provider_map=pmap, package_names=known, cap_index=cap_index
        )
        if srcs:
            sources |= srcs
        else:
            unresolved.add(miss)
    return sources, unresolved


async def _recheck_job_ready(
    db: Any,
    job: BuildJob,
    *,
    plan: LayerPlan,
    release: Release,
    cap_index: RepoCapabilityIndex,
    owner: str,
    repo: str,
    distgit_branch: str,
    releasename: str,
    headers: dict[str, str],
    target_arches: set[str] | None,
) -> None:
    """Re-verify one blocked job against the fresh repo state."""
    from oreon_build.core.rpm_requires import merge_provider_maps

    pkg = await db.get(Package, job.package_id)
    if pkg is None:
        return
    pkg_name = pkg.name
    spec_text = await _refetch_spec_text(
        pkg_name, owner=owner, repo=repo, distgit_branch=distgit_branch, releasename=releasename, headers=headers
    )
    if not spec_text:
        return

    parse_fn = _spec_parse_fn(releasename, target_arches=target_arches)
    parsed = await asyncio.to_thread(parse_fn, spec_text, f"{pkg_name}.spec")
    opt = _plan_options(plan)
    plan_arches = [str(a) for a in (opt.get("architectures") or DEFAULT_ARCH) if a] or list(DEFAULT_ARCH)
    arches = await _capability_arches_for_job(db, job, plan_arches)

    provides_index = await get_distgit_provides_index_cached(
        owner=owner,
        repo=repo,
        distgit_branch=distgit_branch,
        releasename=releasename,
        headers=headers,
    )
    provider_map: dict[str, set[str]] = {}
    package_names: set[str] = set()
    if provides_index is not None:
        provider_map = merge_provider_maps(distgit_providers_dict(provides_index))
        package_names = set(provides_index.package_names)

    still_missing: set[str] = set()
    unresolved_caps: set[str] = set()
    leaf_ok: list[str] = []

    for req in dependency_capabilities(parsed):
        if not req or "%" in req:
            continue
        if cap_index.satisfies_requirement(req):
            leaf_ok.append(req)
            continue
        if _miss_is_self_provided(
            req,
            pkg=pkg_name,
            provider_map=provider_map,
            package_names=package_names,
            cap_index=cap_index,
        ):
            leaf_ok.append(req)
            continue
        srcs = _provider_sources_for_miss(
            req,
            pkg=pkg_name,
            provider_map=provider_map,
            package_names=package_names,
            cap_index=cap_index,
        )
        if srcs:
            still_missing |= srcs
        else:
            hint = _unresolved_hint(req)
            if hint:
                unresolved_caps.add(hint)

    install_srcs, install_gaps = await asyncio.to_thread(
        _resolve_install_holes,
        leaf_ok,
        pkg_name=pkg_name,
        cap_index=cap_index,
        releasename=releasename,
        publish_branch=job.branch,
        architectures=arches,
        provider_map=provider_map,
        package_names=package_names,
    )
    still_missing |= {s for s in install_srcs if s and s != pkg_name}
    for g in install_gaps:
        if cap_index.satisfies_requirement(g):
            continue
        if _miss_is_self_provided(
            g,
            pkg=pkg_name,
            provider_map=provider_map,
            package_names=package_names,
            cap_index=cap_index,
        ):
            continue
        srcs = _provider_sources_for_miss(
            g,
            pkg=pkg_name,
            provider_map=provider_map,
            package_names=package_names,
            cap_index=cap_index,
        )
        if srcs:
            still_missing |= srcs
        else:
            hint = _unresolved_hint(g)
            if hint:
                unresolved_caps.add(hint)

    old_r = await db.execute(select(LayerJobBlocker).where(LayerJobBlocker.build_job_id == job.id))
    for row in old_r.scalars().all():
        await db.delete(row)
    await db.flush()

    if still_missing:
        attached = await _spawn_layer_dep_sources(
            db,
            job,
            plan=plan,
            release=release,
            sources=still_missing,
            owner=owner,
            repo=repo,
            distgit_branch=distgit_branch,
            releasename=releasename,
            headers=headers,
            arches=arches,
            cap_index=cap_index,
            force_build=True,
        )
        leftover = still_missing - attached
        if attached:
            job.status = BuildStatus.BLOCKED
            bits = [f"waiting on: {', '.join(sorted(attached)[:8])}"]
            if leftover:
                bits.append(f"cannot resolve: {', '.join(sorted(leftover)[:4])}")
            job.blocked_reason = "; ".join(bits)[:500]
            await db.flush()
            await resolve_layer_blocker_cycles(
                db, int(plan.id), cap_index=cap_index
            )
            return
        unresolved_caps |= {_unresolved_hint(x) for x in leftover}

    if unresolved_caps:
        job.status = BuildStatus.BLOCKED
        job.blocked_reason = (
            f"cannot resolve: {', '.join(sorted(unresolved_caps)[:8])} (import into distgit)"
        )
        await db.flush()
        return

    job.status = BuildStatus.QUEUED
    job.blocked_reason = None
    await db.flush()


async def _spawn_layer_dep_sources(
    db: Any,
    job: BuildJob,
    *,
    plan: LayerPlan,
    release: Release,
    sources: set[str],
    owner: str,
    repo: str,
    distgit_branch: str,
    releasename: str,
    headers: dict[str, str],
    arches: list[str],
    cap_index: RepoCapabilityIndex | None = None,
    force_build: bool = False,
) -> set[str]:
    """Create/link blocker sources for a job. Returns sources that were attached.

    force_build=True means the capability is already known unsatisfied so never
    skip a distgit provider just because the index thinks the EVR looks current.
    """
    from oreon_build.services.distgit_order import _spec_parse_fn

    attached: set[str] = set()
    roots = {(r or "").strip() for r in (plan.root_packages or []) if (r or "").strip()}
    parse_fn = _spec_parse_fn(releasename, target_arches={a.lower() for a in arches if a} or None)
    for src in sorted(sources):
        existing_r = await db.execute(
            select(BuildJob)
            .join(Package, Package.id == BuildJob.package_id)
            .where(Package.name == src, BuildJob.layer_plan_id == plan.id)
        )
        existing = list(existing_r.scalars().all())
        if existing:
            db.add(LayerJobBlocker(build_job_id=job.id, blocking_source=src))
            attached.add(src)
            for ej in existing:
                if ej.id == job.id:
                    continue
                reason = (ej.blocked_reason or "").strip()
                if ej.status == BuildStatus.CANCELLED:
                    ej.status = BuildStatus.QUEUED
                    ej.completed_at = None
                    ej.blocked_reason = None
                elif ej.status == BuildStatus.FAILED and (
                    parse_missing_provides_from_text(reason)
                    or reason.startswith("expanding missing provides")
                    or reason.startswith("waiting on:")
                ):
                    ej.status = BuildStatus.QUEUED
                    ej.completed_at = None
                    ej.blocked_reason = None
            await db.flush()
            continue
        spec_check = await _refetch_spec_text(
            src, owner=owner, repo=repo, distgit_branch=distgit_branch, releasename=releasename, headers=headers
        )
        if not spec_check:
            continue
        if not force_build and cap_index is not None and src not in roots:
            try:
                parsed = await asyncio.to_thread(parse_fn, spec_check, f"{src}.spec")
            except Exception:
                parsed = None
            if parsed is not None and not _source_needs_build(
                src,
                cap_index=cap_index,
                always_build=roots,
                epoch=int(getattr(parsed, "epoch", 0) or 0),
                version=str(getattr(parsed, "version", "") or ""),
                release=str(getattr(parsed, "release", "") or ""),
                require_evr=True,
            ):
                continue
        new_jobs = await _create_job_for_package(
            db,
            plan,
            src,
            release=release,
            workers_by_arch={normalize_machine_arch(a): True for a in arches},
            architectures=arches,
            distgit_url=_plan_options(plan).get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch=distgit_branch,
            preferred_mock_environment_id=_plan_options(plan).get("preferred_mock_environment_id"),
            priority=int(_plan_options(plan).get("priority") or 0),
            triggered_by_id=plan.triggered_by_id,
            request=None,
            releasename=releasename,
            publish_branch=job.branch,
            layer_index=max(0, (job.layer_index or 0) - 1),
            initial_status=BuildStatus.QUEUED,
            blocked_reason=None,
        )
        if not new_jobs:
            continue
        db.add(LayerJobBlocker(build_job_id=job.id, blocking_source=src))
        attached.add(src)
    return attached


async def _revive_upstream_cancelled_dependents(
    db: Any,
    *,
    plan_id: int,
    source_name: str,
    _seen: set[str] | None = None,
) -> int:
    """Reblock jobs cancelled because source looked failed during missing-provides expand."""
    src = (source_name or "").strip()
    if not src:
        return 0
    seen = _seen if _seen is not None else set()
    if src in seen:
        return 0
    seen.add(src)
    prefix = f"upstream {src} failed"
    soft = f"waiting on: {src} (failed)"
    wait = f"waiting on: {src}"
    jobs_r = await db.execute(
        select(BuildJob)
        .options(selectinload(BuildJob.package))
        .where(
            BuildJob.layer_plan_id == plan_id,
            BuildJob.status.in_((BuildStatus.CANCELLED, BuildStatus.FAILED)),
        )
    )
    revived = 0
    for dep in jobs_r.scalars().all():
        reason = (dep.blocked_reason or "").strip()
        if not (
            reason.startswith(prefix)
            or reason == soft
            or reason.startswith(wait + " (failed)")
            or (reason.startswith("waiting on:") and src in reason and "failed" in reason)
        ):
            continue
        old_r = await db.execute(
            select(LayerJobBlocker).where(LayerJobBlocker.build_job_id == dep.id)
        )
        for row in old_r.scalars().all():
            await db.delete(row)
        db.add(LayerJobBlocker(build_job_id=int(dep.id), blocking_source=src))
        dep.status = BuildStatus.BLOCKED
        dep.completed_at = None
        dep.blocked_reason = f"waiting on: {src}"[:500]
        revived += 1
        dep_name = dep.package.name if dep.package else ""
        if dep_name:
            revived += await _revive_upstream_cancelled_dependents(
                db, plan_id=plan_id, source_name=dep_name, _seen=seen
            )
    if revived:
        await db.flush()
        logger.info(
            "layer_plan_id=%s revived %s cancelled waiter(s) for %s",
            plan_id,
            revived,
            src,
        )
    return revived


async def try_autofix_layer_missing_provides(
    db: Any,
    job: BuildJob,
    reason: str,
    *,
    package_name: str | None = None,
) -> bool:
    """If failure is missing provides with distgit sources, reblock and expand. True if handled."""
    if job.layer_plan_id is None:
        return False
    misses = parse_missing_provides_from_text(reason)
    if not misses:
        misses = await _misses_from_job_build_log(db, job)
    if not misses:
        return False
    plan = await db.get(LayerPlan, job.layer_plan_id, with_for_update=True)
    if plan is None or plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
        return False

    # claim blocked immediately so reconcile wont soft-settle waiters during expand
    prior_status = job.status
    job.status = BuildStatus.BLOCKED
    job.completed_at = None
    job.blocked_reason = "expanding missing provides"
    await db.flush()

    async def _restore_failed() -> None:
        job.status = BuildStatus.FAILED if prior_status == BuildStatus.FAILED else prior_status
        if job.status != BuildStatus.BLOCKED:
            job.blocked_reason = (reason or job.blocked_reason or "")[:500] or job.blocked_reason
        await db.flush()

    release = await db.get(Release, job.release_id)
    if release is None:
        await _restore_failed()
        return False
    opt = _plan_options(plan)
    dg_url = str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    gr = _github_owner_repo_from_git_url(dg_url)
    if not gr:
        await _restore_failed()
        return False
    owner, distgit_repo = gr
    dg_branch = str(opt.get("distgit_branch") or release_distgit_branch(release)).strip()
    plan_arches = [str(a) for a in (opt.get("architectures") or DEFAULT_ARCH) if a] or list(DEFAULT_ARCH)
    arches = await _capability_arches_for_job(db, job, plan_arches)
    releasename = (release.releasename or "").strip()
    headers = _github_headers()

    from oreon_build.core.rpm_requires import merge_provider_maps

    provides_index = await get_distgit_provides_index_cached(
        owner=owner,
        repo=distgit_repo,
        distgit_branch=dg_branch,
        releasename=releasename,
        headers=headers,
    )
    provider_map: dict[str, set[str]] = {}
    package_names: set[str] = set()
    if provides_index is not None:
        provider_map = merge_provider_maps(distgit_providers_dict(provides_index))
        package_names = set(provides_index.package_names)

    cap_index = await asyncio.to_thread(
        load_repo_capability_index,
        releasename,
        job.branch,
        arches,
        extra_branches=default_capability_extra_branches(job.branch),
    )

    pkg_name = package_name or ""
    if not pkg_name:
        pkg = await db.get(Package, job.package_id)
        pkg_name = pkg.name if pkg else "build"

    sources: set[str] = set()
    unresolved: set[str] = set()
    for miss in sorted(misses):
        if cap_index.satisfies_requirement(miss):
            continue
        srcs = _provider_sources_for_miss(
            miss,
            pkg=pkg_name,
            provider_map=provider_map,
            package_names=package_names,
            cap_index=cap_index,
        )
        if srcs:
            sources |= srcs
        else:
            hint = _unresolved_hint(miss)
            if hint:
                unresolved.add(hint)

    if not sources:
        await _restore_failed()
        return False

    old_r = await db.execute(select(LayerJobBlocker).where(LayerJobBlocker.build_job_id == job.id))
    for row in old_r.scalars().all():
        await db.delete(row)
    await db.flush()

    attached = await _spawn_layer_dep_sources(
        db,
        job,
        plan=plan,
        release=release,
        sources=sources,
        owner=owner,
        repo=distgit_repo,
        distgit_branch=dg_branch,
        releasename=releasename,
        headers=headers,
        arches=arches,
        cap_index=cap_index,
        force_build=True,
    )
    if not attached:
        await _restore_failed()
        return False

    attached_list = sorted(attached)
    job.status = BuildStatus.BLOCKED
    job.completed_at = None
    reason_bits = [f"waiting on: {', '.join(attached_list[:8])}"]
    if unresolved:
        reason_bits.append(f"cannot resolve: {', '.join(sorted(unresolved)[:4])}")
    job.blocked_reason = "; ".join(reason_bits)[:500]
    await db.flush()
    await resolve_layer_blocker_cycles(db, int(plan.id), cap_index=cap_index)
    await _revive_upstream_cancelled_dependents(
        db, plan_id=int(plan.id), source_name=pkg_name
    )
    logger.info(
        "layer_plan_id=%s job_id=%s autofix missing provides -> blocked on %s",
        plan.id,
        job.id,
        ",".join(attached_list[:12]),
    )
    return True


async def on_package_published(source_name: str, releasename: str) -> None:
    from oreon_build.db.session import async_session_maker

    async with async_session_maker() as db:
        blocker_r = await db.execute(
            select(LayerJobBlocker.build_job_id)
            .where(LayerJobBlocker.blocking_source == source_name)
            .distinct()
        )
        job_ids = [int(x) for x in blocker_r.scalars().all()]
    if not job_ids:
        return
    clear_repo_capability_cache()

    async with async_session_maker() as db:
        jobs_r = await db.execute(
            select(BuildJob).where(BuildJob.id.in_(job_ids), BuildJob.status == BuildStatus.BLOCKED)
        )
        jobs = list(jobs_r.scalars().all())
        work: list[dict[str, Any]] = []
        for job in jobs:
            if job.layer_plan_id is None:
                continue
            plan = await db.get(LayerPlan, job.layer_plan_id)
            if plan is None or plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING):
                continue
            release = await db.get(Release, job.release_id)
            if release is None:
                continue
            opt = _plan_options(plan)
            dg_url = str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip()
            gr = _github_owner_repo_from_git_url(dg_url)
            if not gr:
                continue
            owner, distgit_repo = gr
            dg_branch = str(opt.get("distgit_branch") or release_distgit_branch(release)).strip()
            plan_arches = [str(a) for a in (opt.get("architectures") or DEFAULT_ARCH) if a] or list(DEFAULT_ARCH)
            arches = await _capability_arches_for_job(db, job, plan_arches)
            work.append(
                {
                    "job_id": int(job.id),
                    "plan_id": int(plan.id),
                    "release_id": int(release.id),
                    "branch": job.branch,
                    "owner": owner,
                    "repo": distgit_repo,
                    "distgit_branch": dg_branch,
                    "arches": arches,
                    "target_arches": {a.lower() for a in arches if a} or None,
                }
            )

    if not work:
        return

    # load cap indexes outside any DB txn (can take minutes)
    cap_by_key: dict[tuple[str, str, tuple[str, ...]], Any] = {}
    for item in work:
        key = (releasename, str(item["branch"] or ""), tuple(item["arches"]))
        if key in cap_by_key:
            continue
        cap_by_key[key] = await asyncio.to_thread(
            load_repo_capability_index,
            releasename,
            item["branch"],
            item["arches"],
            extra_branches=default_capability_extra_branches(item["branch"]),
        )

    headers: dict[str, str] = {}
    for item in work:
        key = (releasename, str(item["branch"] or ""), tuple(item["arches"]))
        cap_index = cap_by_key[key]
        async with async_session_maker() as db:
            job = await db.get(BuildJob, item["job_id"], with_for_update=True)
            if job is None or job.status != BuildStatus.BLOCKED:
                continue
            plan = await db.get(LayerPlan, item["plan_id"])
            release = await db.get(Release, item["release_id"])
            if (
                plan is None
                or release is None
                or plan.status not in (LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING)
            ):
                continue
            try:
                await _recheck_job_ready(
                    db,
                    job,
                    plan=plan,
                    release=release,
                    cap_index=cap_index,
                    owner=item["owner"],
                    repo=item["repo"],
                    distgit_branch=item["distgit_branch"],
                    releasename=releasename,
                    headers=headers,
                    target_arches=item["target_arches"],
                )
                await db.commit()
                logger.info(
                    "layer unblock after publish source=%s job_id=%s -> %s",
                    source_name,
                    job.id,
                    job.status.value,
                )
            except Exception:
                await db.rollback()
                logger.exception(
                    "layer_engine: recheck failed job_id=%s source=%s", item["job_id"], source_name
                )


async def cascade_fail_dependents(
    db: Any,
    job: BuildJob,
    package_name: str,
    reason: str,
    *,
    _seen: set[int] | None = None,
) -> None:
    """Soft-block waiters on a failed/cancelled package. No cascade-cancel.

    Dependents stay BLOCKED with their blocker rows so the scoreboard stays
    accurate. maybe_settle / finalize_blocked_on_dead_upstreams closes them
    once nothing else can run.
    """
    if parse_missing_provides_from_text(reason) or (reason or "").strip().startswith(
        "expanding missing provides"
    ):
        return
    if job.status == BuildStatus.BLOCKED and (job.blocked_reason or "").startswith(
        ("waiting on:", "expanding missing provides")
    ):
        return

    seen = _seen if _seen is not None else set()
    jid = int(job.id)
    if jid in seen:
        return
    seen.add(jid)

    if job.status in (BuildStatus.SUCCESS, BuildStatus.SKIPPED):
        return
    if job.status in (BuildStatus.FAILED, BuildStatus.CANCELLED):
        job.blocked_reason = job.blocked_reason or reason[:500]

    pkg = await db.get(Package, job.package_id)
    src_name = pkg.name if pkg else package_name
    plan_id = job.layer_plan_id
    old_bl = await db.execute(select(LayerJobBlocker).where(LayerJobBlocker.build_job_id == jid))
    for row in old_bl.scalars().all():
        await db.delete(row)
    await db.flush()

    blocker_r = await db.execute(
        select(LayerJobBlocker).where(LayerJobBlocker.blocking_source == src_name)
    )
    dependent_job_ids = {b.build_job_id for b in blocker_r.scalars().all()} - seen
    if not dependent_job_ids:
        return
    dep_q = select(BuildJob).where(
        BuildJob.id.in_(dependent_job_ids),
        BuildJob.status.in_(
            (BuildStatus.BLOCKED, BuildStatus.PENDING, BuildStatus.QUEUED)
        ),
    )
    if plan_id is not None:
        dep_q = dep_q.where(BuildJob.layer_plan_id == plan_id)
    dep_jobs_r = await db.execute(dep_q)
    for dep in dep_jobs_r.scalars().all():
        if int(dep.id) in seen:
            continue
        has = await db.scalar(
            select(LayerJobBlocker.id)
            .where(
                LayerJobBlocker.build_job_id == dep.id,
                LayerJobBlocker.blocking_source == src_name,
            )
            .limit(1)
        )
        if has is None:
            db.add(
                LayerJobBlocker(
                    build_job_id=int(dep.id),
                    blocking_source=src_name,
                )
            )
        dep.status = BuildStatus.BLOCKED
        dep.completed_at = None
        dep.blocked_reason = f"waiting on: {src_name} (failed)"[:500]
        await db.flush()


_ACTIVE_BLOCKER_STATUSES = (
    BuildStatus.PENDING,
    BuildStatus.QUEUED,
    BuildStatus.BLOCKED,
    BuildStatus.RUNNING,
)
_DEAD_BLOCKER_STATUSES = (BuildStatus.FAILED, BuildStatus.CANCELLED)
_OK_BLOCKER_STATUSES = (BuildStatus.SUCCESS, BuildStatus.SKIPPED)


async def finalize_blocked_on_dead_upstreams(db: Any, plan_id: int) -> int:
    """Cancel blocked jobs that only wait on FAILED/CANCELLED packages.

    Walks transitive waiters in rounds so A->B->C closes cleanly. Returns how
    many jobs were finalized.
    """
    changed = 0
    for _ in range(64):
        jobs_r = await db.execute(
            select(BuildJob).where(
                BuildJob.layer_plan_id == plan_id,
                BuildJob.status == BuildStatus.BLOCKED,
            )
        )
        blocked = list(jobs_r.scalars().all())
        if not blocked:
            break
        round_n = 0
        for job in blocked:
            reason = (job.blocked_reason or "").strip()
            if reason.startswith("expanding missing provides"):
                continue
            bl_r = await db.execute(
                select(LayerJobBlocker.blocking_source).where(
                    LayerJobBlocker.build_job_id == job.id
                )
            )
            blockers = [str(x[0]) for x in bl_r.all() if x and x[0]]
            if not blockers:
                continue
            any_dead = False
            any_active = False
            for src in blockers:
                st_r = await db.execute(
                    select(BuildJob.status)
                    .join(Package, Package.id == BuildJob.package_id)
                    .where(BuildJob.layer_plan_id == plan_id, Package.name == src)
                )
                statuses = [s for (s,) in st_r.all()]
                if not statuses:
                    any_active = True
                    break
                if any(s in _ACTIVE_BLOCKER_STATUSES for s in statuses):
                    any_active = True
                    break
                if any(s in _DEAD_BLOCKER_STATUSES for s in statuses):
                    any_dead = True
                elif not all(s in _OK_BLOCKER_STATUSES for s in statuses):
                    any_active = True
                    break
            if any_active or not any_dead:
                continue
            dead_src = blockers[0]
            job.status = BuildStatus.CANCELLED
            job.blocked_reason = f"upstream {dead_src} failed"[:500]
            old_r = await db.execute(
                select(LayerJobBlocker).where(LayerJobBlocker.build_job_id == job.id)
            )
            for row in old_r.scalars().all():
                await db.delete(row)
            await db.flush()
            round_n += 1
        changed += round_n
        if not round_n:
            break
    return changed


_last_blocker_recheck: dict[int, float] = {}
_BLOCKER_RECHECK_COOLDOWN_SEC = 120.0
_FULL_RECHECK_COOLDOWN_SEC = 90.0
_FULL_RECHECK_PER_SWEEP = 8


def _strongly_connected_components(graph: dict[int, set[int]]) -> list[list[int]]:
    index = 0
    stack: list[int] = []
    on_stack: set[int] = set()
    indices: dict[int, int] = {}
    lowlinks: dict[int, int] = {}
    result: list[list[int]] = []

    def strongconnect(v: int) -> None:
        nonlocal index
        indices[v] = index
        lowlinks[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, ()):
            if w not in indices:
                strongconnect(w)
                lowlinks[v] = min(lowlinks[v], lowlinks[w])
            elif w in on_stack:
                lowlinks[v] = min(lowlinks[v], indices[w])
        if lowlinks[v] == indices[v]:
            comp: list[int] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            result.append(comp)

    for v in graph:
        if v not in indices:
            strongconnect(v)
    return result


def deadlock_blocker_cycles(
    blocked_job_ids: list[int],
    job_package: dict[int, str],
    blockers: dict[int, list[str]],
) -> list[list[int]]:
    """SCCs in the blocked-job wait graph that can never make progress."""
    name_to_jobs: dict[str, list[int]] = {}
    for jid in blocked_job_ids:
        name = (job_package.get(jid) or "").strip()
        if name:
            name_to_jobs.setdefault(name, []).append(jid)

    graph: dict[int, set[int]] = {jid: set() for jid in blocked_job_ids}
    for jid in blocked_job_ids:
        for src in blockers.get(jid, []) or []:
            src_name = (src or "").strip()
            if not src_name:
                continue
            for other in name_to_jobs.get(src_name, []):
                graph[jid].add(other)

    cycles: list[list[int]] = []
    for comp in _strongly_connected_components(graph):
        if len(comp) > 1:
            cycles.append(sorted(comp))
        elif len(comp) == 1:
            v = comp[0]
            if v in graph.get(v, ()):
                cycles.append(comp)
    return cycles


def _name_dep_cycles(deps: dict[str, set[str]]) -> list[list[str]]:
    names = set(deps) | {b for bs in deps.values() for b in bs}
    if not names:
        return []
    id_of = {n: i for i, n in enumerate(sorted(names))}
    name_of = {i: n for n, i in id_of.items()}
    graph: dict[int, set[int]] = {id_of[n]: set() for n in names}
    for cons, provs in deps.items():
        if cons not in id_of:
            continue
        for p in provs:
            if p in id_of:
                graph[id_of[cons]].add(id_of[p])
    out: list[list[str]] = []
    for comp in _strongly_connected_components(graph):
        if len(comp) > 1:
            out.append(sorted(name_of[i] for i in comp))
        elif len(comp) == 1:
            v = comp[0]
            if v in graph.get(v, ()):
                out.append([name_of[v]])
    return out


def _pick_cycle_break_edge(deps: dict[str, set[str]], cycle_names: set[str]) -> tuple[str, str] | None:
    """OBS Order-style: break wait on the most-waited package, stable by name."""
    in_deg: dict[str, int] = {n: 0 for n in cycle_names}
    edges: list[tuple[str, str]] = []
    for cons in cycle_names:
        for p in deps.get(cons, ()):
            if p not in cycle_names:
                continue
            edges.append((cons, p))
            in_deg[p] = in_deg.get(p, 0) + 1
    if not edges:
        return None
    edges.sort(key=lambda e: (-in_deg.get(e[1], 0), e[0], e[1]))
    return edges[0]


def break_dep_cycles(
    deps: dict[str, set[str]],
    *,
    repo_current: Any | None = None,
) -> dict[str, set[str]]:
    """Drop circular wait edges. Prefer repo-satisfiable edges, else deterministic break."""
    out: dict[str, set[str]] = {k: set(v) for k, v in deps.items()}

    def _present(name: str) -> bool:
        if repo_current is None:
            return False
        if callable(repo_current):
            try:
                return bool(repo_current(name))
            except Exception:
                return False
        return name in repo_current

    for _ in range(512):
        cycles = _name_dep_cycles(out)
        if not cycles:
            break
        changed = False
        for comp in cycles:
            comp_set = set(comp)
            for cons in comp:
                for p in list(out.get(cons, ())):
                    if p in comp_set and _present(p):
                        out.setdefault(cons, set()).discard(p)
                        changed = True
        if changed:
            continue
        # one Order break per pass
        best: tuple[str, str] | None = None
        best_key: tuple[int, str, str] | None = None
        for comp in cycles:
            edge = _pick_cycle_break_edge(out, set(comp))
            if edge is None:
                continue
            in_deg = sum(1 for c in comp for p in out.get(c, ()) if p == edge[1])
            key = (-in_deg, edge[0], edge[1])
            if best_key is None or key < best_key:
                best_key = key
                best = edge
        if best is None:
            break
        out.setdefault(best[0], set()).discard(best[1])
        logger.info(
            "layer seed/runtime cycle break dropped %s -> %s",
            best[0],
            best[1],
        )
    return out


async def _drop_blocker_sources(db: Any, job: BuildJob, sources: set[str]) -> int:
    if not sources:
        return 0
    rows_r = await db.execute(
        select(LayerJobBlocker).where(LayerJobBlocker.build_job_id == job.id)
    )
    dropped = 0
    remaining: list[str] = []
    for row in rows_r.scalars().all():
        src = (row.blocking_source or "").strip()
        if src in sources:
            await db.delete(row)
            dropped += 1
        elif src:
            remaining.append(src)
    if dropped:
        if remaining:
            status, reason = initial_job_status(set(remaining))
            if job.status == BuildStatus.BLOCKED:
                job.blocked_reason = reason
        else:
            job.status = BuildStatus.QUEUED
            job.blocked_reason = None
        await db.flush()
    return dropped


async def resolve_layer_blocker_cycles(
    db: Any,
    plan_id: int,
    *,
    cap_index: RepoCapabilityIndex | None = None,
) -> int:
    """OBS-style: break circular waits (repo drop / Order), never cascade-cancel."""
    jobs_r = await db.execute(
        select(BuildJob)
        .options(selectinload(BuildJob.package))
        .where(
            BuildJob.layer_plan_id == plan_id,
            BuildJob.status == BuildStatus.BLOCKED,
        )
    )
    blocked = list(jobs_r.scalars().all())
    if not blocked:
        return 0

    job_package = {
        int(j.id): (j.package.name if j.package else f"job-{j.id}") for j in blocked
    }
    jids = list(job_package.keys())
    bl_r = await db.execute(
        select(LayerJobBlocker.build_job_id, LayerJobBlocker.blocking_source).where(
            LayerJobBlocker.build_job_id.in_(jids)
        )
    )
    blockers: dict[int, list[str]] = {jid: [] for jid in jids}
    for jid, src in bl_r.all():
        if jid is None or not src:
            continue
        blockers.setdefault(int(jid), []).append(str(src))

    cycles = deadlock_blocker_cycles(jids, job_package, blockers)
    if not cycles:
        return 0

    by_id = {int(j.id): j for j in blocked}
    touched = 0

    def _repo_ok(name: str) -> bool:
        if cap_index is None:
            return False
        n = (name or "").strip()
        if not n:
            return False
        try:
            return bool(cap_index.source_present_in_repo(n) or n in cap_index.binary_names)
        except Exception:
            return False

    for _pass in range(64):
        active_jids = [
            jid for jid in jids if by_id.get(jid) and by_id[jid].status == BuildStatus.BLOCKED
        ]
        cycles = deadlock_blocker_cycles(active_jids, job_package, blockers)
        if not cycles:
            break

        repo_changed = False
        for comp in cycles:
            comp_names = {job_package.get(jid, "") for jid in comp}
            for jid in comp:
                job = by_id.get(jid)
                if job is None or job.status != BuildStatus.BLOCKED:
                    continue
                drop = {
                    src
                    for src in blockers.get(jid, [])
                    if (src in comp_names and _repo_ok(src))
                    or src == job_package.get(jid, "")
                }
                if not drop:
                    continue
                n = await _drop_blocker_sources(db, job, drop)
                if n:
                    blockers[jid] = [s for s in blockers.get(jid, []) if s not in drop]
                    touched += 1
                    repo_changed = True
                    logger.info(
                        "layer_plan_id=%s job_id=%s cycle repo-break dropped %s",
                        plan_id,
                        jid,
                        sorted(drop)[:8],
                    )
        if repo_changed:
            continue

        order_changed = False
        for comp in cycles:
            comp_names = {job_package.get(jid, "") for jid in comp if job_package.get(jid)}
            name_deps: dict[str, set[str]] = {n: set() for n in comp_names}
            for jid in comp:
                pname = job_package.get(jid, "")
                if not pname:
                    continue
                for src in blockers.get(jid, []):
                    if src in comp_names:
                        name_deps[pname].add(src)
            edge = _pick_cycle_break_edge(name_deps, comp_names)
            if edge is None:
                continue
            waiter_name, blocker_name = edge
            for jid in comp:
                if job_package.get(jid) != waiter_name:
                    continue
                job = by_id.get(jid)
                if job is None or job.status != BuildStatus.BLOCKED:
                    continue
                if blocker_name not in blockers.get(jid, []):
                    continue
                n = await _drop_blocker_sources(db, job, {blocker_name})
                if n:
                    blockers[jid] = [s for s in blockers.get(jid, []) if s != blocker_name]
                    touched += 1
                    order_changed = True
                    logger.info(
                        "layer_plan_id=%s job_id=%s cycle order-break dropped %s -> %s",
                        plan_id,
                        jid,
                        waiter_name,
                        blocker_name,
                    )
            if order_changed:
                break
        if not order_changed:
            break

    return touched


# old name kept for imports during rollout
async def cancel_layer_blocker_cycles(
    db: Any,
    plan_id: int,
    *,
    cap_index: RepoCapabilityIndex | None = None,
) -> int:
    return await resolve_layer_blocker_cycles(db, plan_id, cap_index=cap_index)


async def _cheap_unblock_if_blockers_done(db: Any, job: BuildJob, plan_id: int) -> bool:
    """Queue when blockers succeeded. Soft-cancel only if every blocker was CANCELLED.

    FAILED is not dead here. Autofix may still flip FAILED -> BLOCKED for missing
    provides. Real hard-fail waiters close via finalize_blocked_on_dead_upstreams
    from maybe_settle once nothing is active.
    """
    bl_r = await db.execute(
        select(LayerJobBlocker.blocking_source).where(LayerJobBlocker.build_job_id == job.id)
    )
    blockers = [str(x[0]) for x in bl_r.all() if x and x[0]]
    if not blockers:
        return False

    dead: list[str] = []
    pending = False
    for src in blockers:
        st_r = await db.execute(
            select(BuildJob.status)
            .join(Package, Package.id == BuildJob.package_id)
            .where(BuildJob.layer_plan_id == plan_id, Package.name == src)
        )
        statuses = [s for (s,) in st_r.all()]
        if not statuses:
            pending = True
            continue
        if all(s == BuildStatus.CANCELLED for s in statuses):
            dead.append(src)
            continue
        if any(s != BuildStatus.SUCCESS for s in statuses):
            pending = True

    if dead:
        job.status = BuildStatus.CANCELLED
        job.blocked_reason = f"upstream {dead[0]} failed"[:500]
        old_r = await db.execute(select(LayerJobBlocker).where(LayerJobBlocker.build_job_id == job.id))
        for row in old_r.scalars().all():
            await db.delete(row)
        await db.flush()
        return True

    if pending:
        return False

    old_r = await db.execute(select(LayerJobBlocker).where(LayerJobBlocker.build_job_id == job.id))
    for row in old_r.scalars().all():
        await db.delete(row)
    job.status = BuildStatus.QUEUED
    job.blocked_reason = None
    await db.flush()
    return True


async def reconcile_stuck_layer_blockers() -> None:
    # cheap path first (no cap index). full recheck only for blocked jobs with no blockers.
    import time as _time

    from oreon_build.db.session import async_session_maker

    async with async_session_maker() as db:
        plan_r = await db.execute(
            select(LayerPlan.id)
            .join(BuildJob, BuildJob.layer_plan_id == LayerPlan.id)
            .where(
                LayerPlan.status == LayerPlanStatus.RUNNING,
                BuildJob.status == BuildStatus.BLOCKED,
            )
            .distinct()
        )
        plan_ids = [int(x) for x in plan_r.scalars().all()]

    now = _time.monotonic()
    for plan_id in plan_ids:
        need_full: list[int] = []
        cycle_cap: RepoCapabilityIndex | None = None
        owner = ""
        distgit_repo = ""
        dg_branch = ""
        arches: list[str] = list(DEFAULT_ARCH)
        releasename = ""
        branch = ""
        async with async_session_maker() as db:
            plan = await db.get(LayerPlan, plan_id)
            if plan is None or plan.status != LayerPlanStatus.RUNNING:
                continue
            release = await db.get(Release, plan.release_id)
            if release is None:
                continue
            jobs_r = await db.execute(
                select(BuildJob).where(
                    BuildJob.layer_plan_id == plan_id,
                    BuildJob.status == BuildStatus.BLOCKED,
                )
            )
            blocked = list(jobs_r.scalars().all())
            if not blocked:
                continue
            opt = _plan_options(plan)
            dg_url = str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip()
            gr = _github_owner_repo_from_git_url(dg_url)
            if not gr:
                continue
            owner, distgit_repo = gr
            dg_branch = str(opt.get("distgit_branch") or release_distgit_branch(release)).strip()
            arches = [str(a) for a in (opt.get("architectures") or DEFAULT_ARCH) if a] or list(DEFAULT_ARCH)
            releasename = (release.releasename or "").strip()
            branch = blocked[0].branch

            try:
                # single-arch index for cycle repo-break + stuck recheck (multi-arch OOMs)
                recheck_arches = [arches[0]] if arches else list(DEFAULT_ARCH[:1])
                cycle_cap = await asyncio.to_thread(
                    load_repo_capability_index,
                    releasename,
                    branch,
                    recheck_arches,
                    extra_branches=default_capability_extra_branches(branch),
                )
                n_broke = await resolve_layer_blocker_cycles(db, plan_id, cap_index=cycle_cap)
                if n_broke:
                    await db.commit()
                    from oreon_build.services.layer_plan import maybe_settle_layer_plan

                    await maybe_settle_layer_plan(db, plan_id)
                    await db.commit()
                    plan = await db.get(LayerPlan, plan_id)
                    if plan is None or plan.status != LayerPlanStatus.RUNNING:
                        continue
                    jobs_r = await db.execute(
                        select(BuildJob).where(
                            BuildJob.layer_plan_id == plan_id,
                            BuildJob.status == BuildStatus.BLOCKED,
                        )
                    )
                    blocked = list(jobs_r.scalars().all())
                    if not blocked:
                        continue
            except Exception:
                await db.rollback()
                logger.exception(
                    "layer_engine: cycle resolve failed plan_id=%s", plan_id
                )
                plan = await db.get(LayerPlan, plan_id)
                if plan is None or plan.status != LayerPlanStatus.RUNNING:
                    continue
                jobs_r = await db.execute(
                    select(BuildJob).where(
                        BuildJob.layer_plan_id == plan_id,
                        BuildJob.status == BuildStatus.BLOCKED,
                    )
                )
                blocked = list(jobs_r.scalars().all())
                if not blocked:
                    continue

            for j in blocked:
                jid = int(j.id)
                last = _last_blocker_recheck.get(jid, 0.0)
                try:
                    job = await db.get(BuildJob, jid, with_for_update=True)
                    if job is None or job.status != BuildStatus.BLOCKED:
                        continue
                    if await _cheap_unblock_if_blockers_done(db, job, plan_id):
                        _last_blocker_recheck[jid] = now
                        await db.commit()
                        continue
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.exception(
                        "layer_engine: cheap unblock failed job_id=%s plan_id=%s", jid, plan_id
                    )
                    continue
                bl_r = await db.execute(
                    select(LayerJobBlocker.blocking_source).where(LayerJobBlocker.build_job_id == jid)
                )
                blocker_srcs = [str(x[0]) for x in bl_r.all() if x and x[0]]
                if blocker_srcs:
                    missing_src = False
                    for src in blocker_srcs:
                        n = await db.scalar(
                            select(BuildJob.id)
                            .join(Package, Package.id == BuildJob.package_id)
                            .where(BuildJob.layer_plan_id == plan_id, Package.name == src)
                            .limit(1)
                        )
                        if n is None:
                            missing_src = True
                            break
                    if not missing_src:
                        continue
                if now - last < _FULL_RECHECK_COOLDOWN_SEC:
                    continue
                need_full.append(jid)

            try:
                from oreon_build.services.layer_plan import maybe_settle_layer_plan

                await maybe_settle_layer_plan(db, plan_id)
                await db.commit()
                plan = await db.get(LayerPlan, plan_id)
                if plan is None or plan.status != LayerPlanStatus.RUNNING:
                    continue
            except Exception:
                await db.rollback()
                logger.exception(
                    "layer_engine: settle after cheap unblock failed plan_id=%s", plan_id
                )

        if not need_full:
            continue
        need_full = need_full[:_FULL_RECHECK_PER_SWEEP]

        if cycle_cap is not None:
            cap_index = cycle_cap
        else:
            recheck_arches = [arches[0]] if arches else list(DEFAULT_ARCH[:1])
            cap_index = await asyncio.to_thread(
                load_repo_capability_index,
                releasename,
                branch,
                recheck_arches,
                extra_branches=default_capability_extra_branches(branch),
            )
        headers: dict[str, str] = {}
        target_arches = {a.lower() for a in arches if a} or None
        for jid in need_full:
            _last_blocker_recheck[jid] = now
            async with async_session_maker() as db:
                job = await db.get(BuildJob, jid, with_for_update=True)
                if job is None or job.status != BuildStatus.BLOCKED:
                    continue
                plan = await db.get(LayerPlan, plan_id)
                release = await db.get(Release, job.release_id)
                if plan is None or release is None or plan.status != LayerPlanStatus.RUNNING:
                    continue
                try:
                    await _recheck_job_ready(
                        db,
                        job,
                        plan=plan,
                        release=release,
                        cap_index=cap_index,
                        owner=owner,
                        repo=distgit_repo,
                        distgit_branch=dg_branch,
                        releasename=releasename,
                        headers=headers,
                        target_arches=target_arches,
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.exception(
                        "layer_engine: stuck blocker recheck failed job_id=%s plan_id=%s",
                        jid,
                        plan_id,
                    )

        if len(_last_blocker_recheck) > 5000:
            cutoff = now - (_FULL_RECHECK_COOLDOWN_SEC * 2)
            for k, ts in list(_last_blocker_recheck.items()):
                if ts < cutoff:
                    _last_blocker_recheck.pop(k, None)


async def reconcile_settled_layer_plans() -> None:
    """Safety-net sweep: wake missed publish unblocks, then settle finished plans."""
    from oreon_build.db.session import async_session_maker
    from oreon_build.services.layer_plan import maybe_settle_layer_plan

    try:
        await reconcile_stuck_layer_blockers()
    except Exception:
        logger.exception("layer_engine: stuck blocker reconcile failed")

    async with async_session_maker() as db:
        result = await db.execute(
            select(LayerPlan.id).where(
                LayerPlan.status.in_((LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING))
            )
        )
        ids = [int(x) for x in result.scalars().all()]
        for pid in ids:
            try:
                await maybe_settle_layer_plan(db, pid)
            except Exception:
                logger.exception("layer_engine: reconcile failed layer_plan_id=%s", pid)
                await db.rollback()
        await db.commit()
