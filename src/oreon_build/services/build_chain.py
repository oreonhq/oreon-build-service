"""Distgit chain builds: colon-separated parallel groups, sequential stages, gated on repo publish."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
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
from oreon_build.services.build_target_util import (
    batch_latest_package_version_ids,
    batch_package_ids_by_name,
    get_or_create_build_target,
    latest_package_version_id,
    package_ids_with_success_on_all_targets,
)

if TYPE_CHECKING:
    from oreon_build.models import Account

logger = logging.getLogger(__name__)

DEFAULT_ARCH = ["x86_64", "aarch64"]

# Cache distgit order for chain dep gating (GitHub + spec parse is expensive).
_chain_distgit_order_cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
# Cache repo primary.xml analysis for proactive runtime Requires (R2 fetch).
_chain_repo_runtime_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
# Cache provide_name -> source from repo primary.xml (shared by chain capability resolution).
_chain_repo_provides_cache: dict[tuple[str, str, str], tuple[float, dict[str, set[str]], set[str]]] = {}
_chain_repo_sat_cache: dict[tuple[str, str, str], tuple[float, Any]] = {}


def _chain_use_distgit_dep_gate() -> bool:
    return (os.getenv("OREON_CHAIN_USE_DISTGIT_ORDER_GATE") or "1").strip().lower() not in ("0", "false", "no")


def _chain_prepare_async() -> bool:
    """Run distgit expand + stage-0 enqueue in a background loop (avoids HTTP 524 on chain-trigger)."""
    return (os.getenv("OREON_CHAIN_PREPARE_ASYNC") or "1").strip().lower() not in ("0", "false", "no")


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


def _chain_proactive_repo_runtime_fast() -> bool:
    """Use primary.xml only for proactive runtime scan (skip per-SRPM rpm -qp scans)."""
    return (os.getenv("OREON_CHAIN_PROACTIVE_REPO_RUNTIME_FAST") or "1").strip().lower() not in (
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


async def _resolve_sources_from_package_registry(db: Any, capability: str) -> list[str]:
    """When a capability equals an OBS package name, treat that package as the source."""
    cap = (capability or "").strip()
    if not cap:
        return []
    prow = await db.execute(select(Package.name).where(Package.name == cap).limit(1))
    hit = prow.scalar_one_or_none()
    if hit:
        return [str(hit)]
    return []


def resolve_distgit_sources_for_missing_providers(
    order: Any,
    providers: list[str],
    *,
    repo_provides_index: dict[str, set[str]] | None = None,
    repo_known_sources: set[str] | None = None,
) -> set[str]:
    from oreon_build.services.distgit_order import resolve_distgit_sources_for_capability

    sources: set[str] = set()
    for cap in providers:
        for src in resolve_distgit_sources_for_capability(
            order,
            cap,
            repo_provides_index=repo_provides_index,
            repo_known_sources=repo_known_sources,
        ):
            sources.add(src)
        nevra_name = _rpm_name_from_nevra(cap)
        if nevra_name and nevra_name != cap:
            for src in resolve_distgit_sources_for_capability(
                order,
                nevra_name,
                repo_provides_index=repo_provides_index,
                repo_known_sources=repo_known_sources,
            ):
                sources.add(src)
    return sources


async def _cached_repo_provides_index(
    releasename: str,
    branch: str,
    arch: str,
) -> tuple[dict[str, set[str]], set[str]]:
    """Provides index from the same cached primary.xml parse as repo satisfaction."""
    sat, err = await _cached_repo_satisfaction_index(releasename, branch, arch)
    if err:
        logger.info(
            "repo provides index unavailable for %s/%s/%s: %s",
            releasename,
            branch,
            arch,
            err,
        )
        return {}, set()
    return sat.provides_index, sat.known_sources


async def _cached_repo_satisfaction_index(
    releasename: str,
    branch: str,
    arch: str,
) -> tuple[Any, str | None]:
    from oreon_build.services.dependencies_graph import RepoTarget, load_repo_satisfaction_index

    k = (releasename.strip(), branch.strip(), normalize_machine_arch(arch))
    now = time.monotonic()
    ttl = float((os.getenv("OREON_CHAIN_REPO_RUNTIME_CACHE_SEC") or "120").strip() or "120")
    ttl = max(30.0, min(ttl, 600.0))
    hit = _chain_repo_sat_cache.get(k)
    if hit is not None and (now - hit[0]) < ttl:
        return hit[1], None

    target = RepoTarget(
        releasename=k[0],
        branch=k[1],
        arch=k[2],
        prefix=f"{k[0]}/{k[1]}/{k[2]}",
    )
    repo_sat, err = await asyncio.to_thread(load_repo_satisfaction_index, target)
    _chain_repo_sat_cache[k] = (now, repo_sat)
    return repo_sat, err


async def resolve_chain_sources_for_missing_providers(
    db: Any,
    order: Any,
    providers: list[str],
    *,
    releasename: str | None = None,
    repo_branch: str | None = None,
    repo_arch: str | None = None,
) -> set[str]:
    """
    Resolve mock/dnf missing capabilities to distgit source packages that Provide them.

    Uses parsed spec Provides/subpackage names, published-repo primary.xml, then exact OBS package names.
    """
    repo_pi: dict[str, set[str]] = {}
    repo_known: set[str] = set()
    if releasename and repo_branch and repo_arch:
        repo_pi, repo_known = await _cached_repo_provides_index(releasename, repo_branch, repo_arch)

    sources = resolve_distgit_sources_for_missing_providers(
        order,
        providers,
        repo_provides_index=repo_pi or None,
        repo_known_sources=repo_known or None,
    )
    if sources:
        return sources
    for cap in providers:
        for src in await _resolve_sources_from_package_registry(db, cap):
            sources.add(src)
        nevra_name = _rpm_name_from_nevra(cap)
        if nevra_name and nevra_name != cap:
            for src in await _resolve_sources_from_package_registry(db, nevra_name):
                sources.add(src)
    return sources


async def unresolved_chain_capabilities(
    db: Any,
    order: Any,
    capabilities: list[str],
    *,
    releasename: str | None = None,
    repo_branch: str | None = None,
    repo_arch: str | None = None,
) -> list[str]:
    """Capabilities the chain cannot map to a buildable source package name."""
    out: list[str] = []
    seen: set[str] = set()
    for cap in capabilities:
        c = (cap or "").strip()
        if not c or c in seen:
            continue
        seen.add(c)
        if not await resolve_chain_sources_for_missing_providers(
            db,
            order,
            [c],
            releasename=releasename,
            repo_branch=repo_branch,
            repo_arch=repo_arch,
        ):
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
        "Chain build stopped: OBS could not find a source package to build for a missing "
        "dependency.",
        "",
    ]
    if context and context.strip():
        lines.append(context.strip())
        lines.append("")
    lines.append(f"Release: {releasename}")
    lines.append(f"Specs repo: {distgit_url} (branch {distgit_branch})")
    lines.append("")
    if not caps:
        lines.append("No capability name was recorded.")
    else:
        lines.append("Capabilities not Provided by any spec in this release tree:")
        for cap in caps:
            lines.append(f"  - {cap}")
    lines.append("")
    lines.append(
        "Chain resolution scans every spec for Provides: tags and %package subpackage names "
        "(with %global macro expansion), then published repo metadata. If the capability is "
        "missing, add Provides: <capability> on the subpackage that should supply it, or queue "
        "that source package earlier. Dependencies only in Fedora need mock lookaside repos."
    )
    return "\n".join(lines)


def record_chain_unresolved_capabilities(
    chain: BuildChain,
    capabilities: list[str],
    *,
    releasename: str,
    distgit_url: str,
    distgit_branch: str,
    context: str | None = None,
) -> str:
    """Store the full capability list on the chain and return the formatted error message."""
    caps = sorted({(c or "").strip() for c in capabilities if (c or "").strip()})
    opt = dict(_options_dict(chain))
    opt["unresolved_capabilities"] = caps
    _set_chain_options(chain, opt)
    return format_chain_unresolved_providers_message(
        caps,
        releasename=releasename,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        context=context,
    )


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


def _earliest_stage_index_for_packages(
    stages: list[list[str]],
    package_names: set[str],
) -> int | None:
    want = {str(n).lower() for n in package_names if n}
    if not want:
        return None
    for idx, stage in enumerate(stages):
        for pkg in stage:
            if str(pkg).lower() in want:
                return idx
    return None


def _isolate_packages_into_prior_stage(
    stages: list[list[str]],
    stage_index: int,
    package_names: set[str],
) -> list[list[str]] | None:
    """Move ``package_names`` out of ``stages[stage_index]`` into a new stage inserted before it."""
    if stage_index < 0 or stage_index >= len(stages):
        return None
    want = {str(n).lower() for n in package_names if n}
    current = [str(p) for p in stages[stage_index] if p]
    isolated = sorted(p for p in current if p.lower() in want)
    if not isolated:
        return None
    rest = [p for p in current if p.lower() not in want]
    before = [list(s) for s in stages[:stage_index]]
    after = [list(s) for s in stages[stage_index + 1 :]]
    if rest:
        return before + [isolated, rest] + after
    return before + [isolated] + after


def _capability_in_repo_provides(cap: str, repo_pi: dict[str, set[str]]) -> bool:
    c = (cap or "").strip()
    if not c:
        return False
    for key in (c, c.lower()):
        if repo_pi.get(key):
            return True
    return False


def format_chain_repo_capability_still_missing_message(
    capabilities: list[str],
    built_sources: list[str],
    *,
    releasename: str,
    distgit_url: str,
    distgit_branch: str,
    failed_package: str | None = None,
) -> str:
    caps = sorted({(c or "").strip() for c in capabilities if (c or "").strip()})
    srcs = sorted({(s or "").strip() for s in built_sources if (s or "").strip()})
    lines = [
        "Chain build stopped: a dependency RPM is already in your Oreon repo (or was built "
        "successfully on this chain), but mock/dnf still cannot satisfy a capability name.",
        "",
    ]
    if failed_package:
        lines.append(f"Failed package: {failed_package}")
        lines.append("")
    lines.append(f"Release: {releasename}")
    lines.append(f"Specs repo: {distgit_url} (branch {distgit_branch})")
    lines.append("")
    if srcs:
        lines.append("Source package(s) already built for this chain:")
        for s in srcs[:12]:
            lines.append(f"  - {s}")
        lines.append("")
    lines.append("Capabilities still missing from published repo metadata:")
    for cap in caps:
        lines.append(f"  - {cap}")
    lines.append("")
    lines.append(
        "The built RPM likely does not Provide the capability name dnf requested. Add a "
        "Provides: entry in the spec for that capability on the subpackage that should supply "
        "it, rebuild the source package(s) listed above, then resume the chain."
    )
    return "\n".join(lines)


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


async def _chain_package_still_needs_build(
    db: Any,
    chain: BuildChain,
    package_name: str,
    *,
    architectures: list[str],
    preferred_mock_environment_id: int | None,
    republish_needed: set[str] | None = None,
    order: Any | None = None,
    repo_sat: Any | None = None,
) -> bool:
    """True when this chain still needs to build ``package_name``."""
    still = await _batch_chain_packages_still_needing_build(
        db,
        chain,
        [package_name],
        architectures=architectures,
        preferred_mock_environment_id=preferred_mock_environment_id,
        republish_needed=republish_needed,
        order=order,
        repo_sat=repo_sat,
    )
    return package_name in still


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
) -> set[int]:
    """package_ids that do not yet have SUCCESS on every chain architecture for pinned spec versions."""
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
        jobs_created_on_or_after=None,
    )
    return {pid for pid in ids if pid not in complete_ids}


async def _batch_package_version_ids_for_spec(
    db: Any,
    name_to_id: dict[str, int],
    spec_evrs: dict[str, str],
) -> dict[str, int | None]:
    """Map package name -> package_version_id for the distgit spec EVR (None if no row yet)."""
    if not name_to_id:
        return {}
    wanted_versions = {
        spec_evrs.get(name) for name in name_to_id if spec_evrs.get(name)
    }
    wanted_versions.discard(None)
    if not wanted_versions:
        return {name: None for name in name_to_id}

    rows = await db.execute(
        select(PackageVersion.package_id, PackageVersion.id, PackageVersion.version).where(
            PackageVersion.package_id.in_(list(name_to_id.values())),
            PackageVersion.version.in_(list(wanted_versions)),
        )
    )
    by_pkg_ver: dict[tuple[int, str], int] = {}
    for pkg_id, vid, ver in rows.all():
        by_pkg_ver[(int(pkg_id), str(ver))] = int(vid)

    out: dict[str, int | None] = {}
    for name, pkg_id in name_to_id.items():
        spec_ver = spec_evrs.get(name)
        if not spec_ver:
            out[name] = None
            continue
        out[name] = by_pkg_ver.get((pkg_id, spec_ver))
    return out


async def _batch_chain_packages_still_needing_build(
    db: Any,
    chain: BuildChain,
    package_names: list[str],
    *,
    architectures: list[str],
    preferred_mock_environment_id: int | None,
    republish_needed: set[str] | None,
    order: Any | None = None,
    repo_sat: Any | None = None,
) -> set[str]:
    """Package names that still need a chain build (repo + spec version + DB success)."""
    from oreon_build.services.chain_satisfaction import (
        chain_target_needs_build,
        source_published_at_least_spec,
    )

    still: set[str] = set()
    if republish_needed:
        still |= {n for n in package_names if n in republish_needed}
    to_lookup = [n for n in package_names if n and n not in still]
    if not to_lookup:
        return still

    spec_evrs: dict[str, str] = (
        order.spec_evrs if order is not None and isinstance(getattr(order, "spec_evrs", None), dict) else {}
    )
    user_targets = _chain_user_target_names(chain)
    name_to_id = await batch_package_ids_by_name(db, to_lookup)
    pv_by_name = await _batch_package_version_ids_for_spec(db, name_to_id, spec_evrs)

    check_ids: list[int] = []
    id_to_name: dict[int, str] = {}
    for name in to_lookup:
        if name not in name_to_id:
            still.add(name)
            continue
        pid = name_to_id[name]
        is_user = name.lower() in user_targets
        if repo_sat is not None and spec_evrs and not is_user:
            if source_published_at_least_spec(name, spec_evrs, repo_sat):
                continue
        if is_user and repo_sat is not None and spec_evrs:
            if not chain_target_needs_build(name, spec_evrs, repo_sat):
                pv_id = pv_by_name.get(name)
                if pv_id is None:
                    still.add(name)
                    continue
                check_ids.append(pid)
                id_to_name[pid] = name
                continue
        pv_id = pv_by_name.get(name)
        if pv_id is None:
            still.add(name)
            continue
        check_ids.append(pid)
        id_to_name[pid] = name

    if not check_ids:
        return still

    version_by_package = {pid: pv_by_name.get(id_to_name[pid]) for pid in check_ids}
    lacking_ids = await _package_ids_still_needing_chain_build(
        db,
        chain,
        check_ids,
        architectures=architectures,
        preferred_mock_environment_id=preferred_mock_environment_id,
        version_by_package=version_by_package,
    )
    for pid in lacking_ids:
        still.add(id_to_name[pid])
    return still


def _chain_root_package_names(chain: BuildChain) -> list[str]:
    """Package names the user put in this chain (all stages), used to scope distgit parsing."""
    seen: set[str] = set()
    roots: list[str] = []
    stages = chain.stages_json if isinstance(chain.stages_json, list) else []
    for stage in stages:
        for p in stage:
            n = str(p).strip()
            if not n:
                continue
            key = n.lower()
            if key in seen:
                continue
            seen.add(key)
            roots.append(n)
    if roots:
        return roots
    try:
        for grp in parse_chain_build_spec(chain.chain_spec or ""):
            for n in grp:
                n = str(n).strip()
                if not n:
                    continue
                key = n.lower()
                if key in seen:
                    continue
                seen.add(key)
                roots.append(n)
    except ValueError:
        pass
    return roots


@dataclass
class _ChainExpandContext:
    """Fresh distgit + repo metadata loaded once per prepare (shared across expand rounds)."""

    releasename: str
    dg_url: str
    dg_br: str
    publish_branch: str
    canon_arch: str
    arches: list[str]
    pref_pm: int | None
    order: Any
    repo_pi: dict[str, set[str]]
    repo_known: set[str]
    repo_sat: Any
    runtime_analysis: dict[str, Any]


async def _build_chain_expand_context(
    db: Any,
    chain: BuildChain,
    release: Release,
    *,
    dg_url: str,
    dg_br: str,
    arches: list[str],
    pref_pm: int | None,
) -> _ChainExpandContext | None:
    releasename = (release.releasename or "").strip()
    if not releasename:
        return None
    publish_branch = r2_publish_branch(release)
    canon_arch = normalize_machine_arch(arches[0] if arches else "x86_64")

    from oreon_build.services.dependencies_graph import (
        RepoTarget,
        analyze_repo_runtime_primary_only,
        load_repo_satisfaction_index,
    )
    from oreon_build.services.distgit_order import compute_distgit_build_order_for_chain

    roots = _chain_root_package_names(chain)
    t0 = time.monotonic()
    order = await compute_distgit_build_order_for_chain(
        releasename=releasename,
        distgit_url=dg_url,
        distgit_branch=dg_br,
        root_packages=roots,
    )
    logger.info(
        "chain_id=%s distgit closure from %s root(s), %s specs parsed in %.1fs",
        chain.id,
        len(roots),
        len(order.package_names or []),
        time.monotonic() - t0,
    )

    target = RepoTarget(
        releasename=releasename,
        branch=publish_branch,
        arch=canon_arch,
        prefix=f"{releasename}/{publish_branch}/{canon_arch}",
    )
    t1 = time.monotonic()
    repo_sat, sat_err = await asyncio.to_thread(load_repo_satisfaction_index, target)
    if sat_err:
        logger.warning("chain_id=%s repo satisfaction: %s", chain.id, sat_err)
    logger.info("chain_id=%s repo primary.xml (fresh) in %.1fs", chain.id, time.monotonic() - t1)

    spec_caps = order.capability_providers if isinstance(order.capability_providers, dict) else {}
    runtime_analysis: dict[str, Any] = {}
    if _chain_proactive_repo_runtime():
        t2 = time.monotonic()
        if _chain_proactive_repo_runtime_fast():
            runtime_analysis = await asyncio.to_thread(
                analyze_repo_runtime_primary_only,
                target,
                spec_capability_providers=spec_caps,
            )
            mode = "fast-primary"
        else:
            from oreon_build.services.dependencies_graph import _analyze_repo

            runtime_analysis = await asyncio.to_thread(
                _analyze_repo,
                target,
                None,
                spec_caps,
            )
            mode = "full-srpm"
        logger.info(
            "chain_id=%s proactive runtime scan (%s, fresh) in %.1fs",
            chain.id,
            mode,
            time.monotonic() - t2,
        )

    return _ChainExpandContext(
        releasename=releasename,
        dg_url=dg_url,
        dg_br=dg_br,
        publish_branch=publish_branch,
        canon_arch=canon_arch,
        arches=arches,
        pref_pm=pref_pm,
        order=order,
        repo_pi=repo_sat.provides_index,
        repo_known=repo_sat.known_sources,
        repo_sat=repo_sat,
        runtime_analysis=runtime_analysis,
    )


def invalidate_distgit_order_cache(
    releasename: str,
    distgit_url: str,
    distgit_branch: str,
) -> None:
    """Drop cached spec-tree order so the next chain step sees fresh distgit specs."""
    k = (releasename.strip(), distgit_url.strip(), distgit_branch.strip())
    _chain_distgit_order_cache.pop(k, None)


def invalidate_repo_chain_caches(releasename: str, branch: str, arch: str) -> None:
    """Drop cached repodata indexes after a publish or spec Provides change."""
    k = (releasename.strip(), branch.strip(), normalize_machine_arch(arch))
    _chain_repo_provides_cache.pop(k, None)
    _chain_repo_sat_cache.pop(k, None)
    _chain_repo_runtime_cache.pop(k, None)


async def _cached_distgit_build_order(
    releasename: str,
    distgit_url: str,
    distgit_branch: str,
    *,
    force_refresh: bool = False,
) -> Any:
    from oreon_build.services.distgit_order import compute_distgit_build_order

    k = (releasename.strip(), distgit_url.strip(), distgit_branch.strip())
    now = time.monotonic()
    ttl = _chain_distgit_order_cache_ttl_sec()
    if not force_refresh:
        hit = _chain_distgit_order_cache.get(k)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
    order = await compute_distgit_build_order(releasename=k[0], distgit_url=k[1], distgit_branch=k[2])
    _chain_distgit_order_cache[k] = (now, order)
    return order


def _sources_missing_repo_capabilities(
    order: Any,
    repo_pi: dict[str, set[str]],
    capabilities: list[str],
) -> set[str]:
    """Source packages that should Provide a capability but published repo metadata does not yet."""
    from oreon_build.services.distgit_order import resolve_distgit_sources_for_capability

    need_rebuild: set[str] = set()
    for cap in capabilities:
        c = (cap or "").strip()
        if not c or _capability_in_repo_provides(c, repo_pi):
            continue
        for src in resolve_distgit_sources_for_capability(
            order,
            c,
            repo_provides_index=repo_pi or None,
        ):
            need_rebuild.add(src)
    return need_rebuild


async def _cached_repo_runtime_analysis(
    releasename: str,
    branch: str,
    arch: str,
    *,
    spec_capability_providers: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    from oreon_build.services.dependencies_graph import (
        RepoTarget,
        _analyze_repo,
        analyze_repo_runtime_primary_only,
    )

    k = (releasename.strip(), branch.strip(), normalize_machine_arch(arch))
    now = time.monotonic()
    ttl = float((os.getenv("OREON_CHAIN_REPO_RUNTIME_CACHE_SEC") or "120").strip() or "120")
    ttl = max(30.0, min(ttl, 600.0))
    hit = _chain_repo_runtime_cache.get(k)
    if hit is not None and (now - hit[0]) < ttl:
        return hit[1]

    target = RepoTarget(
        releasename=k[0],
        branch=k[1],
        arch=k[2],
        prefix=f"{k[0]}/{k[1]}/{k[2]}",
    )
    if _chain_proactive_repo_runtime_fast():
        analysis = await asyncio.to_thread(
            analyze_repo_runtime_primary_only,
            target,
            spec_capability_providers=spec_capability_providers or None,
        )
    else:
        analysis = await asyncio.to_thread(
            _analyze_repo,
            target,
            None,
            spec_capability_providers or None,
        )
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
    repo_pi: dict[str, set[str]] | None = None,
    repo_known: set[str] | None = None,
    repo_sat: Any | None = None,
    analysis: dict[str, Any] | None = None,
) -> tuple[set[str], list[str]]:
    """
    Before any mock run, use repodata + spec Provides metadata to queue source packages that
    fix known broken runtime Requires in the published repo.
    """
    if not _chain_proactive_repo_runtime():
        return set(), []

    releasename = (release.releasename or "").strip()
    if not releasename:
        return set(), []

    branch = r2_publish_branch(release)
    canon_arch = normalize_machine_arch(architectures[0] if architectures else "x86_64")
    if repo_pi is None or repo_known is None:
        repo_pi, repo_known = await _cached_repo_provides_index(releasename, branch, canon_arch)
    repo_known = repo_known or set()
    if repo_sat is None:
        repo_sat, _ = await _cached_repo_satisfaction_index(releasename, branch, canon_arch)
    from oreon_build.services.chain_satisfaction import _requirement_status
    spec_caps = order.capability_providers if isinstance(order.capability_providers, dict) else {}
    if analysis is None:
        analysis = await _cached_repo_runtime_analysis(
            releasename,
            branch,
            canon_arch,
            spec_capability_providers=spec_caps,
        )
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
    scope = (os.getenv("OREON_CHAIN_PROACTIVE_REPO_RUNTIME_SCOPE") or "chain").strip().lower()

    items: list[tuple[str, str, str, str]] = []
    caps_to_resolve: set[str] = set()
    for item in analysis.get("runtime_missing") or []:
        if not isinstance(item, dict):
            continue
        consumer = str(item.get("package") or "")
        req = str(item.get("requirement") or "")
        prov = str(item.get("provider") or "")
        if scope == "chain" and consumer and consumer not in related:
            continue
        req_line = req or prov
        if req_line and _requirement_status(req_line, repo_sat)[0] == "satisfied":
            continue
        for candidate in (prov, req):
            if not candidate:
                continue
            caps_to_resolve.add(candidate)
            items.append((consumer, req, prov, candidate))

    cap_to_sources: dict[str, set[str]] = {}
    for cap in caps_to_resolve:
        cap_to_sources[cap] = resolve_distgit_sources_for_missing_providers(
            order,
            [cap],
            repo_provides_index=repo_pi or None,
            repo_known_sources=repo_known or None,
        )
    registry_caps: set[str] = {c for c, srcs in cap_to_sources.items() if not srcs}
    if registry_caps:
        reg_names = set(registry_caps)
        for cap in list(registry_caps):
            nevra = _rpm_name_from_nevra(cap)
            if nevra and nevra != cap:
                reg_names.add(nevra)
        reg_hits = await batch_package_ids_by_name(db, reg_names)
        for cap in registry_caps:
            if cap in reg_hits:
                cap_to_sources[cap] = {cap}
            else:
                nevra = _rpm_name_from_nevra(cap)
                if nevra and nevra in reg_hits:
                    cap_to_sources[cap] = {nevra}

    srcs_to_check: set[str] = set()
    pending: list[tuple[str, str, set[str]]] = []
    sources: set[str] = set()
    unmapped: set[str] = set()
    for consumer, req, _prov, candidate in items:
        mapped = cap_to_sources.get(candidate) or set()
        if not mapped:
            if consumer and consumer in chain_names:
                unmapped.add(candidate)
            continue
        cap_to_check = req or candidate
        for src in mapped:
            if src not in spec_names:
                continue
            srcs_to_check.add(src)
            pending.append((src, cap_to_check, mapped))

    still_need = await _batch_chain_packages_still_needing_build(
        db,
        chain,
        sorted(srcs_to_check),
        architectures=architectures,
        preferred_mock_environment_id=preferred_mock_environment_id,
        republish_needed=None,
        order=order,
        repo_sat=repo_sat,
    )
    for src, cap_to_check, _mapped in pending:
        if src in still_need:
            sources.add(src)
            continue
        if cap_to_check and not _capability_in_repo_provides(cap_to_check, repo_pi):
            sources.add(src)
    if sources:
        logger.info(
            "chain_id=%s proactive repo runtime deps: %s",
            chain.id,
            sorted(sources),
        )
    return sources, sorted(unmapped)


async def _expand_chain_stages_dependencies(
    db: Any,
    chain: BuildChain,
    stage_index: int,
    *,
    ctx: _ChainExpandContext | None = None,
) -> bool:
    """Repeat distgit + repo-runtime injection until the stage list stops growing."""
    for round_n in range(_chain_expand_max_rounds()):
        before = json.dumps(chain.stages_json if isinstance(chain.stages_json, list) else [])
        ok = await _inject_missing_dep_layers_before(db, chain, stage_index, ctx=ctx)
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
    required_capabilities: list[str] | None = None,
    ctx: _ChainExpandContext | None = None,
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

    if ctx is not None:
        order = ctx.order
        repo_pi = ctx.repo_pi
        repo_known = ctx.repo_known
        repo_sat_index = ctx.repo_sat
        analysis = ctx.runtime_analysis
        publish_branch = ctx.publish_branch
        canon_arch = ctx.canon_arch
        pref_pm = ctx.pref_pm if ctx.pref_pm is not None else pref_pm
    else:
        from oreon_build.services.dependencies_graph import (
            RepoTarget,
            analyze_repo_runtime_primary_only,
            load_repo_satisfaction_index,
        )
        from oreon_build.services.distgit_order import compute_distgit_build_order_for_chain

        publish_branch = r2_publish_branch(release)
        canon_arch = normalize_machine_arch(arches[0] if arches else "x86_64")
        order = await compute_distgit_build_order_for_chain(
            releasename=releasename,
            distgit_url=dg_url,
            distgit_branch=dg_br,
            root_packages=_chain_root_package_names(chain),
        )
        spec_caps = order.capability_providers if isinstance(order.capability_providers, dict) else {}
        target = RepoTarget(
            releasename=releasename,
            branch=publish_branch,
            arch=canon_arch,
            prefix=f"{releasename}/{publish_branch}/{canon_arch}",
        )
        repo_sat_index, _ = await asyncio.to_thread(load_repo_satisfaction_index, target)
        repo_pi = repo_sat_index.provides_index
        repo_known = repo_sat_index.known_sources
        analysis = {}
        if _chain_proactive_repo_runtime():
            if _chain_proactive_repo_runtime_fast():
                analysis = await asyncio.to_thread(
                    analyze_repo_runtime_primary_only,
                    target,
                    spec_capability_providers=spec_caps,
                )
            else:
                from oreon_build.services.dependencies_graph import _analyze_repo

                analysis = await asyncio.to_thread(_analyze_repo, target, None, spec_caps)

    caps_needed = list(required_capabilities or [])
    republish_needed = _sources_missing_repo_capabilities(order, repo_pi, caps_needed)

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
        repo_pi=repo_pi,
        repo_known=repo_known,
        repo_sat=repo_sat_index,
        analysis=analysis,
    )
    if repo_unmapped:
        ctx = (
            "A package in your chain spec has a published runtime dependency that no "
            f"distgit spec Provides (example missing capability: {repo_unmapped[0]})."
        )
        chain.status = BuildChainStatus.FAILED
        chain.error_message = record_chain_unresolved_capabilities(
            chain,
            repo_unmapped,
            releasename=releasename,
            distgit_url=dg_url,
            distgit_branch=dg_br,
            context=ctx,
        )[:8000]
        logger.error("chain_id=%s unmapped repo runtime deps: %s", chain.id, repo_unmapped)
        return False
    merged_extra = set(extra_roots or set()) | proactive_roots

    all_chain_pkg_names: set[str] = {str(p).lower() for stage in stages for p in stage if p}

    from oreon_build.services.chain_satisfaction import (
        compute_chain_dependency_build_set,
        format_chain_version_conflict_message,
    )

    roots = list(pkgs)
    if merged_extra:
        roots.extend(sorted(merged_extra))
    user_targets = _chain_user_target_names(chain)
    dep_plan = compute_chain_dependency_build_set(
        roots,
        order,
        repo_sat_index,
        user_targets,
    )
    if dep_plan.conflicts:
        chain.status = BuildChainStatus.FAILED
        chain.error_message = format_chain_version_conflict_message(
            dep_plan.conflicts,
            releasename=releasename,
            distgit_url=dg_url,
            distgit_branch=dg_br,
        )[:8000]
        logger.error(
            "chain_id=%s version conflicts: %s",
            chain.id,
            [c.requirement for c in dep_plan.conflicts[:5]],
        )
        return False

    candidate_list = sorted(
        p for p in dep_plan.sources_to_build if str(p).lower() not in all_chain_pkg_names
    )
    to_inject: set[str] = set()
    still_needing = await _batch_chain_packages_still_needing_build(
        db,
        chain,
        candidate_list,
        architectures=arches,
        preferred_mock_environment_id=pref_pm,
        republish_needed=republish_needed,
        order=order,
        repo_sat=repo_sat_index,
    )
    for pred in candidate_list:
        pred_lower = pred.lower()
        # Already listed on this chain (even if not built yet). Do not insert duplicate
        # stages each expand round; enqueue/rewind handles packages that still need builds.
        if pred_lower in all_chain_pkg_names:
            continue
        if pred not in spec_names:
            continue
        if pred not in still_needing:
            continue
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


async def _chain_sources_lacking_success(
    db: Any,
    chain: BuildChain,
    source_names: set[str],
    *,
    architectures: list[str],
    preferred_mock_environment_id: int | None,
    order: Any | None = None,
    repo_pi: dict[str, set[str]] | None = None,
    repo_sat: Any | None = None,
    required_capabilities: list[str] | None = None,
) -> set[str]:
    lacking: set[str] = set()
    if order is not None and repo_pi is not None and required_capabilities:
        lacking |= _sources_missing_repo_capabilities(order, repo_pi, required_capabilities)
    names = sorted(n for n in source_names if n not in lacking)
    lacking |= await _batch_chain_packages_still_needing_build(
        db,
        chain,
        names,
        architectures=architectures,
        preferred_mock_environment_id=preferred_mock_environment_id,
        republish_needed=None,
        order=order,
        repo_sat=repo_sat,
    )
    return lacking


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


async def _cancel_chain_jobs_from_stage(
    db: Any,
    chain: BuildChain,
    from_stage_index: int,
    note: str,
) -> None:
    jres = await db.execute(
        select(BuildJob).where(
            BuildJob.chain_id == chain.id,
            BuildJob.chain_stage_index >= from_stage_index,
            BuildJob.status.in_(
                (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING),
            ),
        )
    )
    for job in jres.scalars().all():
        await _cancel_job_and_running_attempts(db, job, note)


async def _rewind_chain_to_stage(
    db: Any,
    chain: BuildChain,
    rewind_stage: int,
    *,
    note: str,
) -> None:
    await _cancel_chain_jobs_from_stage(db, chain, rewind_stage, note)
    chain.active_stage_index = rewind_stage
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
        rewind_stage,
        architectures=arches,
        skip_complete_arches=bool(opt.get("skip_complete_arches", True)),
        distgit_url=str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git"),
        distgit_branch=str(opt.get("distgit_branch") or "main"),
        preferred_mock_environment_id=pref_pm,
        priority=int(opt.get("priority") or 0),
        triggered_by_id=chain.triggered_by_id,
        request=None,
    )


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
    providers: list[str] = []
    releasename = ""
    dg_url = "https://github.com/oreonhq/rpm-specfiles.git"
    dg_br = "main"
    repo_branch = ""
    canon_arch = "x86_64"
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
                arches = [str(x) for x in opt.get("architectures") or DEFAULT_ARCH]
                canon_arch = normalize_machine_arch(arches[0] if arches else "x86_64")
                repo_branch = (
                    r2_publish_branch(release) if release else str(opt.get("branch") or "dev")
                )
                if providers:
                    invalidate_distgit_order_cache(releasename, dg_url, dg_br)
                    invalidate_repo_chain_caches(releasename, repo_branch, canon_arch)
                    order = await _cached_distgit_build_order(
                        releasename, dg_url, dg_br, force_refresh=True
                    )
                    missing_in_distgit = await unresolved_chain_capabilities(
                        db,
                        order,
                        providers,
                        releasename=releasename,
                        repo_branch=repo_branch,
                        repo_arch=canon_arch,
                    )
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
                            record_chain_unresolved_capabilities(
                                chain,
                                missing_in_distgit,
                                releasename=releasename,
                                distgit_url=dg_url,
                                distgit_branch=dg_br,
                                context=ctx,
                            )[:8000],
                            terminal=BuildChainStatus.FAILED,
                        )
                        return True
                    extra_roots = await resolve_chain_sources_for_missing_providers(
                        db,
                        order,
                        providers,
                        releasename=releasename,
                        repo_branch=repo_branch,
                        repo_arch=canon_arch,
                    )
                    if extra_roots:
                        opt = dict(opt)
                        opt["dnf_recovery_count"] = recovery_n + 1
                        _set_chain_options(chain, opt)
                        logger.info(
                            "chain_id=%s dnf recovery: providers %s -> distgit sources %s",
                            chain.id,
                            providers,
                            sorted(extra_roots),
                        )

    ok = await _inject_missing_dep_layers_before(
        db,
        chain,
        stage_idx,
        extra_roots=extra_roots or None,
        required_capabilities=providers or None,
    )
    if not ok:
        return False

    stages_after = chain.stages_json if isinstance(chain.stages_json, list) else []
    if len(stages_after) <= stage_count_before:
        if not extra_roots:
            return False
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
        stages_now = chain.stages_json if isinstance(chain.stages_json, list) else []
        stage_names = (
            {str(p).lower() for p in stages_now[stage_idx]}
            if 0 <= stage_idx < len(stages_now)
            else set()
        )
        same_stage = {s for s in extra_roots if s.lower() in stage_names}
        if same_stage:
            isolated = _isolate_packages_into_prior_stage(stages_now, stage_idx, same_stage)
            if isolated:
                chain.stages_json = isolated
                await db.flush()
                stages_now = isolated
                logger.info(
                    "chain_id=%s isolated parallel deps %s into stage %s before retry",
                    chain.id,
                    sorted(same_stage),
                    stage_idx,
                )

        order_for_check = await _cached_distgit_build_order(releasename, dg_url, dg_br)
        repo_pi: dict[str, set[str]] = {}
        repo_sat_check = None
        if releasename and repo_branch:
            repo_pi, _ = await _cached_repo_provides_index(releasename, repo_branch, canon_arch)
            repo_sat_check, _ = await _cached_repo_satisfaction_index(
                releasename, repo_branch, canon_arch
            )
        lacking = await _chain_sources_lacking_success(
            db,
            chain,
            extra_roots,
            architectures=arches,
            preferred_mock_environment_id=pref_pm,
            order=order_for_check,
            repo_pi=repo_pi,
            repo_sat=repo_sat_check,
            required_capabilities=providers,
        )
        if lacking:
            rewind = _earliest_stage_index_for_packages(stages_now, lacking)
            if rewind is None:
                return False
            await _rewind_chain_to_stage(
                db,
                chain,
                rewind,
                note="Chain rewinding to build missing dependencies",
            )
            logger.info(
                "chain_id=%s recovered from job %s by rewinding to stage %s for %s",
                chain.id,
                failed_job.id,
                rewind,
                sorted(lacking),
            )
            return True

        if providers and releasename and repo_branch:
            still_missing = [c for c in providers if not _capability_in_repo_provides(c, repo_pi)]
            if still_missing:
                if not extra_roots:
                    pkg_r = await db.execute(
                        select(Package.name).where(Package.id == failed_job.package_id)
                    )
                    pkg_name = pkg_r.scalar_one_or_none() or "?"
                    ctx = (
                        f"Build of {pkg_name} failed in mock; no distgit spec Provides "
                        f"{still_missing[0]}."
                    )
                    await fail_chain(
                        db,
                        chain,
                        record_chain_unresolved_capabilities(
                            chain,
                            still_missing,
                            releasename=releasename,
                            distgit_url=dg_url,
                            distgit_branch=dg_br,
                            context=ctx,
                        )[:8000],
                        terminal=BuildChainStatus.FAILED,
                    )
                    return True
                fallback = _sources_missing_repo_capabilities(
                    order_for_check, repo_pi, still_missing
                ) or extra_roots
                rewind = _earliest_stage_index_for_packages(stages_now, fallback)
                if rewind is not None:
                    await _rewind_chain_to_stage(
                        db,
                        chain,
                        rewind,
                        note="Chain rewinding to republish dependency with updated Provides",
                    )
                    logger.info(
                        "chain_id=%s republish rewind stage %s for %s (capabilities %s)",
                        chain.id,
                        rewind,
                        sorted(fallback),
                        still_missing,
                    )
                    return True
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
        "version": (spec_version or "0.0.1").strip() or "0.0.1",
    }


async def _ensure_spec_package_version(
    db: Any,
    package_id: int,
    spec_canonical: str | None,
) -> int | None:
    """Return PackageVersion id for distgit spec EVR, creating a row when the spec was bumped."""
    if not spec_canonical:
        return await latest_package_version_id(db, package_id)
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


async def cancel_chain_by_user(
    db: Any,
    request: Request,
    user: "Account",
    *,
    chain_id: int,
) -> BuildChain:
    """Stop a pending, running, or still-preparing chain and cancel active jobs."""
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
    opt = dict(_options_dict(chain))
    opt.pop("prepare_pending", None)
    opt.pop("prepare_running", None)
    opt.pop("prepare_started_at", None)
    _set_chain_options(chain, opt)
    _chain_prepare_inflight.discard(chain_id)
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


def _chain_user_target_names(chain: BuildChain) -> set[str]:
    """Lowercase package names the user listed in chain_spec (not auto-injected deps)."""
    opt = _options_dict(chain)
    raw = opt.get("chain_user_targets")
    if isinstance(raw, list):
        return {str(n).lower() for n in raw if n}
    try:
        groups = parse_chain_build_spec(chain.chain_spec or "")
    except ValueError:
        return set()
    return {str(n).lower() for grp in groups for n in grp if n}


def _skip_jobs_not_before_for_package(
    chain: BuildChain,
    package_name: str,
    skip_jobs_not_before: datetime | None,
) -> datetime | None:
    """
    Chain-start skip window applies only to user-named targets.

    Auto-injected dependencies use any prior successful build on all arches so we do
    not queue kernel (etc.) again when it was built before this chain started.
    """
    if skip_jobs_not_before is None:
        return None
    if package_name.lower() in _chain_user_target_names(chain):
        return skip_jobs_not_before
    return None


def _set_chain_options(chain: BuildChain, options: dict[str, Any]) -> None:
    """Persist chain_options (SQLAlchemy does not track in-place JSON dict mutation)."""
    chain.chain_options = dict(options)
    flag_modified(chain, "chain_options")


def _chain_prepare_stale_seconds() -> int:
    raw = (os.getenv("OREON_CHAIN_PREPARE_STALE_SEC") or "900").strip()
    try:
        sec = int(raw)
    except ValueError:
        sec = 900
    return max(120, min(sec, 7200))


def _parse_iso_datetime(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


_chain_prepare_inflight: set[int] = set()


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
    distgit_order: Any | None = None,
    repo_sat: Any | None = None,
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

    order = distgit_order
    repo_sat_index = repo_sat
    spec_evrs: dict[str, str] = {}
    if skip_complete_arches and releasename:
        if order is None:
            from oreon_build.services.distgit_order import compute_distgit_build_order_for_chain

            order = await compute_distgit_build_order_for_chain(
                releasename=releasename,
                distgit_url=distgit_url,
                distgit_branch=distgit_branch,
                root_packages=_chain_root_package_names(chain),
            )
        spec_evrs = order.spec_evrs if isinstance(getattr(order, "spec_evrs", None), dict) else {}
        if repo_sat_index is None:
            from oreon_build.services.dependencies_graph import RepoTarget, load_repo_satisfaction_index

            canon = normalize_machine_arch(architectures[0] if architectures else "x86_64")
            target = RepoTarget(
                releasename=releasename,
                branch=publish_branch,
                arch=canon,
                prefix=f"{releasename}/{publish_branch}/{canon}",
            )
            repo_sat_index, _ = await asyncio.to_thread(load_repo_satisfaction_index, target)

    still_need_names: set[str] = set()
    if skip_complete_arches:
        still_need_names = await _batch_chain_packages_still_needing_build(
            db,
            chain,
            pkgs,
            architectures=architectures,
            preferred_mock_environment_id=preferred_mock_environment_id,
            republish_needed=None,
            order=order,
            repo_sat=repo_sat_index,
        )

    for pkg_name in pkgs:
        existing_r = await db.execute(select(Package).where(Package.name == pkg_name))
        existing_pkg = existing_r.scalar_one_or_none()
        package_id: int
        package_version_id: int | None
        pref_mock: int | None

        spec_ver = spec_evrs.get(pkg_name) if spec_evrs else None

        if existing_pkg:
            package_id = existing_pkg.id
            package_version_id = await _ensure_spec_package_version(db, package_id, spec_ver)
            # Chain-specified mock takes priority; fall back to package's stored preference.
            pref_mock = preferred_mock_environment_id or existing_pkg.preferred_mock_environment_id
        else:
            npd = distgit_new_package_dict(
                pkg_name,
                releasename,
                distgit_url=distgit_url,
                distgit_branch=distgit_branch,
                preferred_mock_environment_id=preferred_mock_environment_id,
                spec_version=spec_ver,
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


def _chain_options_preparing(opt: dict[str, Any]) -> bool:
    return bool(opt.get("prepare_pending") or opt.get("prepare_running"))


async def _complete_chain_prepare(
    db: Any,
    chain: BuildChain,
    *,
    triggered_by_id: int | None,
) -> list[BuildJob]:
    """Distgit dependency expand and stage-0 enqueue (may take minutes on large releases)."""
    if chain.status != BuildChainStatus.PENDING:
        return []
    opt = _options_dict(chain)
    dg_url = str(opt.get("distgit_url") or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    dg_branch = str(opt.get("distgit_branch") or "main").strip()
    arches = list(opt.get("architectures") or DEFAULT_ARCH)
    skip_complete_arches = bool(opt.get("skip_complete_arches", True))
    preferred_mock_environment_id = opt.get("preferred_mock_environment_id")
    if preferred_mock_environment_id is not None:
        try:
            preferred_mock_environment_id = int(preferred_mock_environment_id)
        except (TypeError, ValueError):
            preferred_mock_environment_id = None
    else:
        preferred_mock_environment_id = None
    priority = int(opt.get("priority") or 0)

    expand_ctx: _ChainExpandContext | None = None
    if _chain_use_distgit_dep_gate():
        rr = await db.execute(select(Release).where(Release.id == chain.release_id))
        release = rr.scalar_one_or_none()
        if release:
            reln = (release.releasename or "").strip()
            if release and reln:
                pm_raw = opt.get("preferred_mock_environment_id")
                pref_pm: int | None
                if pm_raw is None or pm_raw == "":
                    pref_pm = None
                else:
                    try:
                        pref_pm = int(pm_raw)
                    except (TypeError, ValueError):
                        pref_pm = None
                t_expand = time.monotonic()
                expand_ctx = await _build_chain_expand_context(
                    db,
                    chain,
                    release,
                    dg_url=dg_url,
                    dg_br=dg_branch,
                    arches=arches,
                    pref_pm=pref_pm,
                )
                ok = await _expand_chain_stages_dependencies(db, chain, 0, ctx=expand_ctx)
                logger.info(
                    "chain_id=%s dependency expand finished in %.1fs",
                    chain.id,
                    time.monotonic() - t_expand,
                )
            else:
                ok = await _expand_chain_stages_dependencies(db, chain, 0)
        else:
            ok = await _expand_chain_stages_dependencies(db, chain, 0)
        if not ok:
            chain.status = BuildChainStatus.FAILED
            return []
        if chain.status != BuildChainStatus.PENDING:
            return []

    return await _enqueue_chain_stage_jobs(
        db,
        chain,
        0,
        architectures=arches,
        skip_complete_arches=skip_complete_arches,
        distgit_url=dg_url,
        distgit_branch=dg_branch,
        preferred_mock_environment_id=preferred_mock_environment_id,
        priority=priority,
        triggered_by_id=triggered_by_id,
        request=None,
        distgit_order=expand_ctx.order if expand_ctx else None,
        repo_sat=expand_ctx.repo_sat if expand_ctx else None,
    )


async def _discover_chains_needing_prepare(db: Any) -> list[int]:
    """Chains that still need background distgit expand + stage-0 enqueue."""
    if not (_chain_prepare_async() and _chain_use_distgit_dep_gate()):
        return []
    now = datetime.now(timezone.utc)
    stale_sec = _chain_prepare_stale_seconds()
    result = await db.execute(select(BuildChain).where(BuildChain.status == BuildChainStatus.PENDING))
    out: list[int] = []
    for chain in result.scalars():
        opt = _options_dict(chain)
        if opt.get("prepare_finished_at"):
            continue
        if opt.get("prepare_running"):
            started = _parse_iso_datetime(opt.get("prepare_started_at"))
            if started and (now - started).total_seconds() > stale_sec:
                out.append(chain.id)
            continue
        if opt.get("prepare_pending"):
            out.append(chain.id)
            continue
        job_n = await db.scalar(
            select(func.count()).select_from(BuildJob).where(BuildJob.chain_id == chain.id)
        )
        if int(job_n or 0) == 0:
            out.append(chain.id)
    return out


async def _run_one_chain_prepare(chain_id: int) -> None:
    from oreon_build.db.session import async_session_maker

    try:
        async with async_session_maker() as db:
            result = await db.execute(
                select(BuildChain).where(BuildChain.id == chain_id).with_for_update()
            )
            chain = result.scalar_one_or_none()
            if not chain or chain.status != BuildChainStatus.PENDING:
                return
            opt = dict(_options_dict(chain))
            if opt.get("prepare_finished_at"):
                return
            now_iso = datetime.now(timezone.utc).isoformat()
            if opt.get("prepare_running"):
                started = _parse_iso_datetime(opt.get("prepare_started_at"))
                stale = (
                    started is None
                    or (datetime.now(timezone.utc) - started).total_seconds()
                    > _chain_prepare_stale_seconds()
                )
                if not stale:
                    return
                logger.warning(
                    "chain_id=%s prepare_running stale, retrying background prepare",
                    chain_id,
                )
            opt["prepare_pending"] = True
            opt["prepare_running"] = True
            opt["prepare_started_at"] = now_iso
            _set_chain_options(chain, opt)
            await db.commit()

        logger.info("chain_id=%s background prepare started", chain_id)
        prepare_t0 = time.monotonic()
        async with async_session_maker() as db:
            result = await db.execute(
                select(BuildChain).where(BuildChain.id == chain_id).with_for_update()
            )
            chain = result.scalar_one_or_none()
            if not chain:
                return
            jobs = await _complete_chain_prepare(db, chain, triggered_by_id=chain.triggered_by_id)
            prepare_elapsed = time.monotonic() - prepare_t0
            if chain.status != BuildChainStatus.PENDING:
                opt = dict(_options_dict(chain))
                opt.pop("prepare_pending", None)
                opt.pop("prepare_running", None)
                _set_chain_options(chain, opt)
                await db.commit()
                return
            opt = dict(_options_dict(chain))
            opt.pop("prepare_pending", None)
            opt.pop("prepare_running", None)
            opt["prepare_finished_at"] = datetime.now(timezone.utc).isoformat()
            opt["prepare_stage0_jobs"] = len(jobs)
            _set_chain_options(chain, opt)
            await log_audit(
                db,
                "build.chain_prepare_done",
                account_id=chain.triggered_by_id,
                resource_type="build_chain",
                resource_id=str(chain.id),
                details=f"stage0_jobs={len(jobs)}",
                ip_address=None,
                user_agent="chain-prepare-loop",
            )
            if chain.status == BuildChainStatus.FAILED:
                logger.warning(
                    "chain_id=%s background prepare failed: %s",
                    chain.id,
                    chain.error_message,
                )
            else:
                logger.info(
                    "chain_id=%s background prepare done (%s stage-0 job(s)) in %.1fs",
                    chain.id,
                    len(jobs),
                    prepare_elapsed,
                )
            await db.commit()
    except Exception as exc:
        logger.exception("chain_id=%s background prepare error", chain_id)
        async with async_session_maker() as db:
            result = await db.execute(
                select(BuildChain).where(BuildChain.id == chain_id).with_for_update()
            )
            chain = result.scalar_one_or_none()
            if not chain:
                return
            opt = dict(_options_dict(chain))
            opt.pop("prepare_running", None)
            opt["prepare_pending"] = True
            opt["prepare_last_error"] = f"{exc!s}"[:500]
            _set_chain_options(chain, opt)
            await db.commit()
    finally:
        _chain_prepare_inflight.discard(chain_id)


async def prepare_pending_chains() -> None:
    """Queue background tasks for chains that need distgit expand + stage-0 enqueue."""
    from oreon_build.db.session import async_session_maker

    async with async_session_maker() as db:
        candidate_ids = await _discover_chains_needing_prepare(db)
    for chain_id in candidate_ids:
        if chain_id in _chain_prepare_inflight:
            continue
        _chain_prepare_inflight.add(chain_id)
        asyncio.create_task(_run_one_chain_prepare(chain_id), name=f"chain-prepare-{chain_id}")


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

    user_targets = sorted({str(n).lower() for grp in stages for n in grp if n})
    chain_options: dict[str, Any] = {
        "architectures": arches,
        "skip_complete_arches": skip_complete_arches,
        "distgit_url": dg_url,
        "distgit_branch": dg_branch,
        "preferred_mock_environment_id": preferred_mock_environment_id,
        "priority": priority,
        "chain_user_targets": user_targets,
        # Only successes from jobs created on/after this instant count toward
        # skip_complete_arches for user-named targets (avoids stale all-arch skips).
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

    if _chain_use_distgit_dep_gate() and _chain_prepare_async():
        chain_options["prepare_pending"] = True
        _set_chain_options(chain, chain_options)
        await db.flush()
        logger.info(
            "chain_id=%s queued for background prepare (async chain-trigger)",
            chain.id,
        )
        return chain, [], True

    if _chain_use_distgit_dep_gate():
        reln = (release.releasename or "").strip()
        if reln:
            invalidate_distgit_order_cache(reln, dg_url, dg_branch)
            invalidate_repo_chain_caches(
                reln,
                r2_publish_branch(release),
                normalize_machine_arch(arches[0] if arches else "x86_64"),
            )
        ok = await _expand_chain_stages_dependencies(db, chain, 0)
        if not ok:
            await db.flush()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=chain.error_message or "Chain build could not resolve dependencies",
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
) -> tuple[BuildChain, list[BuildJob], bool]:
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
