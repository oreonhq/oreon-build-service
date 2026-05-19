"""Distgit chain builds: colon-separated parallel groups, sequential stages, gated on repo publish."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
# Cache repo primary.xml analysis for proactive runtime Requires (R2 fetch).
_chain_repo_runtime_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def _chain_use_distgit_dep_gate() -> bool:
    return (os.getenv("OREON_CHAIN_USE_DISTGIT_ORDER_GATE") or "1").strip().lower() not in ("0", "false", "no")


def _chain_use_dnf_provider_recovery() -> bool:
    return (os.getenv("OREON_CHAIN_DNF_PROVIDER_RECOVERY") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _chain_proactive_repo_runtime() -> bool:
    """Scan published repo primary.xml at chain expand time for broken runtime Requires."""
    return (os.getenv("OREON_CHAIN_PROACTIVE_REPO_RUNTIME") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _chain_expand_max_rounds() -> int:
    raw = (os.getenv("OREON_CHAIN_EXPAND_MAX_ROUNDS") or "12").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 12
    return max(1, min(n, 64))


def _chain_dnf_recovery_max_per_chain() -> int:
    raw = (os.getenv("OREON_CHAIN_DNF_RECOVERY_MAX") or "32").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 32
    return max(1, min(n, 256))


_RE_NOTHING_PROVIDES = re.compile(
    r"\bnothing provides ([^\n]+?) needed by\b",
    re.IGNORECASE,
)


def _rpm_name_from_nevra(token: str) -> str:
    s = (token or "").strip()
    if not s:
        return ""
    if s.endswith(".rpm"):
        s = s[:-4]
    parts = s.rsplit("-", 2)
    if len(parts) == 3 and parts[1] and parts[1][0].isdigit():
        return parts[0]
    return s


def parse_dnf_missing_providers(text: str) -> list[str]:
    """Extract capability names from mock/dnf 'nothing provides … needed by' lines."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _RE_NOTHING_PROVIDES.finditer(text):
        cap = m.group(1).strip()
        if not cap:
            continue
        cap = cap.split(",", 1)[0].strip()
        if cap and cap not in seen:
            seen.add(cap)
            out.append(cap)
    return out


def resolve_distgit_sources_for_missing_providers(
    order: Any,
    providers: list[str],
) -> set[str]:
    from oreon_build.services.distgit_order import resolve_distgit_sources_for_capability

    sources: set[str] = set()
    for cap in providers:
        for src in resolve_distgit_sources_for_capability(order, cap):
            sources.add(src)
        nevra_name = _rpm_name_from_nevra(cap)
        if nevra_name and nevra_name != cap:
            for src in resolve_distgit_sources_for_capability(order, nevra_name):
                sources.add(src)
    return sources


def unresolved_distgit_capabilities(order: Any, capabilities: list[str]) -> list[str]:
    """Capabilities that do not map to any source package in the distgit spec tree."""
    out: list[str] = []
    seen: set[str] = set()
    for cap in capabilities:
        c = (cap or "").strip()
        if not c or c in seen:
            continue
        seen.add(c)
        if not resolve_distgit_sources_for_missing_providers(order, [c]):
            out.append(c)
    return sorted(out)


def format_chain_unresolved_providers_message(
    capabilities: list[str],
    *,
    releasename: str,
    distgit_url: str,
    distgit_branch: str,
    context: str | None = None,
) -> str:
    caps = sorted({(c or "").strip() for c in capabilities if (c or "").strip()})
    lines = [
        "Chain stopped: a required dependency is not in your distgit specfiles tree, "
        "so OBS cannot queue a build for it.",
        "",
    ]
    if context and context.strip():
        lines.append(context.strip())
        lines.append("")
    lines.append(f"Spec tree: {distgit_url} (branch {distgit_branch})")
    lines.append(f"Release path: {releasename}/")
    lines.append("")
    if not caps:
        lines.append("No capability name was recorded.")
    else:
        lines.append("Not found in distgit (no source spec Provides these names):")
        for cap in caps[:15]:
            lines.append(f"  - {cap}")
        if len(caps) > 15:
            lines.append(f"  … and {len(caps) - 15} more")
    lines.append("")
    lines.append(
        "Add a package under that release in distgit, or add mock lookaside repos "
        "on this mock environment if the dependency only exists in Fedora."
    )
    return "\n".join(lines)


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


async def _cached_repo_runtime_analysis(releasename: str, branch: str, arch: str) -> dict[str, Any]:
    from oreon_build.services.dependencies_graph import RepoTarget, _analyze_repo

    k = (releasename.strip(), branch.strip(), normalize_machine_arch(arch))
    now = time.monotonic()
    ttl = float((os.getenv("OREON_CHAIN_REPO_RUNTIME_CACHE_SEC") or "120").strip() or "120")
    hit = _chain_repo_runtime_cache.get(k)
    if hit is not None and (now - hit[0]) < max(30.0, min(ttl, 600.0)):
        return hit[1]
    target = RepoTarget(
        releasename=k[0],
        branch=k[1],
        arch=k[2],
        prefix=f"{k[0]}/{k[1]}/{k[2]}",
    )
    analysis = await asyncio.to_thread(_analyze_repo, target)
    _chain_repo_runtime_cache[k] = (now, analysis)
    return analysis


async def _proactive_extra_roots_from_repo_runtime(
    db: Any,
    chain: BuildChain,
    release: Release,
    order: Any,
    *,
    architectures: list[str],
    preferred_mock_environment_id: int | None,
    skip_jobs_not_before: datetime | None,
) -> tuple[set[str], list[str]]:
    """
    Before any mock run, use repodata + distgit Provides map to queue source packages that
    fix known broken runtime Requires in the published repo (e.g. python3-test needs
    python-setuptools-wheel while setuptools source is not built yet).
    """
    if not _chain_proactive_repo_runtime():
        return set(), []

    releasename = (release.releasename or "").strip()
    if not releasename:
        return set(), []

    branch = r2_publish_branch(release)
    arch = architectures[0] if architectures else "x86_64"
    analysis = await _cached_repo_runtime_analysis(releasename, branch, arch)
    if analysis.get("error"):
        logger.info(
            "chain_id=%s proactive repo scan skipped (%s)",
            chain.id,
            analysis.get("error"),
        )
        return set(), []

    spec_names = {str(n) for n in (order.package_names or []) if n}
    stages: list[list[str]] = chain.stages_json if isinstance(chain.stages_json, list) else []
    chain_names = {str(p) for stage in stages for p in stage if p}
    deps_map = _deps_map_from_edges(order.edges if isinstance(order.edges, list) else [])
    related = chain_names | _transitive_spec_predecessors(deps_map, list(chain_names), spec_names)
    scope = (os.getenv("OREON_CHAIN_PROACTIVE_REPO_RUNTIME_SCOPE") or "wide").strip().lower()

    sources: set[str] = set()
    unmapped: set[str] = set()
    for item in analysis.get("runtime_missing") or []:
        if not isinstance(item, dict):
            continue
        consumer = str(item.get("package") or "")
        req = str(item.get("requirement") or "")
        prov = str(item.get("provider") or "")
        if scope == "chain" and consumer and consumer not in related:
            continue
        for candidate in (prov, req):
            if not candidate:
                continue
            mapped = resolve_distgit_sources_for_missing_providers(order, [candidate])
            if not mapped:
                if consumer and consumer in related:
                    unmapped.add(candidate)
                continue
            for src in mapped:
                if src not in spec_names:
                    continue
                pid = await _package_id_for_name(db, src)
                if pid is not None and await _chain_has_successful_build_all_arches(
                    db,
                    chain,
                    pid,
                    architectures=architectures,
                    preferred_mock_environment_id=preferred_mock_environment_id,
                    skip_jobs_not_before=skip_jobs_not_before,
                ):
                    continue
                sources.add(src)
    if sources:
        logger.info(
            "chain_id=%s proactive repo runtime deps: %s",
            chain.id,
            sorted(sources),
        )
    return sources, sorted(unmapped)


async def _expand_chain_stages_dependencies(db: Any, chain: BuildChain, stage_index: int) -> bool:
    """Repeat distgit + repo-runtime injection until the stage list stops growing."""
    for round_n in range(_chain_expand_max_rounds()):
        before = json.dumps(chain.stages_json if isinstance(chain.stages_json, list) else [])
        ok = await _inject_missing_dep_layers_before(db, chain, stage_index)
        if not ok:
            return False
        after = json.dumps(chain.stages_json if isinstance(chain.stages_json, list) else [])
        if after == before:
            return True
        logger.info(
            "chain_id=%s dependency expand round %s grew stages before index %s",
            chain.id,
            round_n + 1,
            stage_index,
        )
    chain.status = BuildChainStatus.FAILED
    chain.error_message = (
        "Chain dependency expansion did not converge (too many missing-dependency rounds). "
        "Narrow the chain spec or fix the distgit/repo graph."
    )
    return False


async def _failure_text_for_chain_recovery(db: Any, failed_job: BuildJob, reason: str) -> str:
    parts: list[str] = []
    if reason and reason.strip():
        parts.append(reason.strip())
    att_r = await db.execute(
        select(BuildAttempt)
        .where(BuildAttempt.build_job_id == failed_job.id)
        .order_by(BuildAttempt.id.desc())
        .limit(1)
    )
    att = att_r.scalar_one_or_none()
    if att and (att.error_message or "").strip():
        parts.append(att.error_message.strip())
    return "\n".join(parts)


async def _inject_missing_dep_layers_before(
    db: Any,
    chain: BuildChain,
    stage_index: int,
    *,
    extra_roots: set[str] | None = None,
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

    proactive_roots, repo_unmapped = await _proactive_extra_roots_from_repo_runtime(
        db,
        chain,
        release,
        order,
        architectures=arches,
        preferred_mock_environment_id=pref_pm,
        skip_jobs_not_before=skip_not_before,
    )
    if repo_unmapped:
        ctx = (
            "Published repo metadata lists runtime dependencies that are not satisfied "
            f"(example consumer: {repo_unmapped[0]})."
        )
        chain.status = BuildChainStatus.FAILED
        chain.error_message = format_chain_unresolved_providers_message(
            repo_unmapped,
            releasename=releasename,
            distgit_url=dg_url,
            distgit_branch=dg_br,
            context=ctx,
        )[:4000]
        logger.error("chain_id=%s unmapped repo runtime deps: %s", chain.id, repo_unmapped)
        return False
    merged_extra = set(extra_roots or set()) | proactive_roots

    deps_map = _deps_map_from_edges(edges)
    all_chain_pkg_names: set[str] = {str(p).lower() for stage in stages for p in stage if p}
    past_stage_pkg_names: set[str] = {
        str(p).lower() for stage in stages[:stage_index] for p in stage if p
    }

    roots = list(pkgs)
    if merged_extra:
        roots.extend(sorted(merged_extra))
    candidates = _transitive_spec_predecessors(deps_map, roots, spec_names)
    if merged_extra:
        candidates.update(s for s in merged_extra if s in spec_names)
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
    reason: str,
) -> bool:
    """
    On a stage failure, inject missing distgit deps before the active stage and
    re-queue that stage instead of failing the whole chain.

    Uses distgit BuildRequires edges plus dnf log lines of the form
    ``nothing provides CAPABILITY needed by …``, resolved via spec Provides metadata.
    """
    if failed_job.chain_stage_index is None:
        return False
    stage_idx = int(failed_job.chain_stage_index)
    stages_before = chain.stages_json if isinstance(chain.stages_json, list) else []
    stage_count_before = len(stages_before)

    extra_roots: set[str] = set()
    if _chain_use_dnf_provider_recovery():
        opt = _options_dict(chain)
        recovery_n = int(opt.get("dnf_recovery_count") or 0)
        if recovery_n < _chain_dnf_recovery_max_per_chain():
            rr = await db.execute(select(Release).where(Release.id == chain.release_id))
            release = rr.scalar_one_or_none()
            releasename = (release.releasename or "").strip() if release else ""
            if releasename:
                dg_url = str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git")
                dg_br = str(opt.get("distgit_branch") or "main")
                fail_text = await _failure_text_for_chain_recovery(db, failed_job, reason)
                providers = parse_dnf_missing_providers(fail_text)
                if providers:
                    order = await _cached_distgit_build_order(releasename, dg_url, dg_br)
                    missing_in_distgit = unresolved_distgit_capabilities(order, providers)
                    if missing_in_distgit:
                        pkg_r = await db.execute(
                            select(Package.name).where(Package.id == failed_job.package_id)
                        )
                        pkg_name = pkg_r.scalar_one_or_none() or "?"
                        ctx = (
                            f"Build of {pkg_name} failed in mock because dnf could not install "
                            "a dependency package already in your Oreon repo."
                        )
                        await fail_chain(
                            db,
                            chain,
                            format_chain_unresolved_providers_message(
                                missing_in_distgit,
                                releasename=releasename,
                                distgit_url=dg_url,
                                distgit_branch=dg_br,
                                context=ctx,
                            ),
                            terminal=BuildChainStatus.FAILED,
                        )
                        return True
                    extra_roots = resolve_distgit_sources_for_missing_providers(order, providers)
                    if extra_roots:
                        opt = dict(opt)
                        opt["dnf_recovery_count"] = recovery_n + 1
                        chain.chain_options = opt
                        logger.info(
                            "chain_id=%s dnf recovery: providers %s -> distgit sources %s",
                            chain.id,
                            providers,
                            sorted(extra_roots),
                        )

    ok = await _inject_missing_dep_layers_before(
        db, chain, stage_idx, extra_roots=extra_roots or None
    )
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
    if await _try_recover_chain_after_failure(db, chain, job, reason):
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
        ok = await _expand_chain_stages_dependencies(db, chain, nxt)
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
        ok = await _expand_chain_stages_dependencies(db, chain, 0)
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
