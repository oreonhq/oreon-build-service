from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from oreon_build.core.rpm_requires import (
    _providers_from_maps,
    is_chain_plan_requirement,
    merge_provider_maps,
    sources_for_requirement,
)
from oreon_build.core.spec_bcond import (
    default_distro_macros,
    parse_spec_bconds,
)
from oreon_build.services.distgit_order import (
    _distgit_source_needers,
    _github_owner_repo_from_git_url,
    _kahn_layers,
    _parse_spec_text,
    _provider_map_from_specs,
    _repo_transitive_miss_sources,
    dependency_capabilities,
    distgit_providers_dict,
    expand_chain_spec_closure_sparse,
    expand_unsatisfied_build_dep_sources,
    fetch_repo_transitive_extras_for_roots,
    get_distgit_provides_index_cached,
    iter_plan_capabilities,
)
from oreon_build.services.repo_capability_index import RepoCapabilityIndex, load_repo_capability_index

logger = logging.getLogger(__name__)


def _merge_provider_maps(*maps: dict[str, set[str]] | None) -> dict[str, set[str]]:
    return merge_provider_maps(*maps)


def _chain_covers_requirement(
    requirement: str,
    repo: RepoCapabilityIndex,
    chain: set[str],
    provider_maps: list[dict[str, set[str]]],
) -> bool:
    if repo.satisfies_requirement(requirement):
        return True
    return any(s in chain for s in sources_for_requirement(requirement, repo, provider_maps=provider_maps))


def _uncovered_plan_capabilities(
    parsed: Any,
    bcond: Any,
    repo: RepoCapabilityIndex,
    chain: set[str],
    provider_maps: list[dict[str, set[str]]] | None = None,
) -> list[str]:
    pmap = provider_maps or []
    uncovered: list[str] = []
    for cap in iter_plan_capabilities(parsed, bcond, repo):
        if not is_chain_plan_requirement(cap):
            continue
        if _chain_covers_requirement(cap, repo, chain, pmap):
            continue
        if repo.satisfies_requirement(cap):
            continue
        providers = sources_for_requirement(cap, repo, provider_maps=pmap)
        if providers:
            uncovered.append(cap)
        else:
            uncovered.append(f"{cap} (no distgit provider)")
    return uncovered


@dataclass
class PlannedPackage:
    name: str
    action: str
    build_profile: str
    rpmbuild_defines: dict[str, str]
    next_profile: str | None = None
    reason: str = ""


@dataclass
class BuildPlanResult:
    releasename: str
    repo_branch: str
    layers: list[list[PlannedPackage]]
    chain_spec: str
    package_plans: dict[str, dict[str, Any]]
    skipped: list[str]
    errors: list[str]
    blocking_errors: list[str] = None  # type: ignore[assignment]
    cycle_warning: bool = False

    def __post_init__(self) -> None:
        if self.blocking_errors is None:
            self.blocking_errors = []


def _plan_slots_for_package(
    name: str,
    spec_text: str,
    repo: RepoCapabilityIndex,
    *,
    parsed: Any | None = None,
    force_build: bool,
    repo_caps: set[str] | None = None,
    chain_sources: set[str] | None = None,
    provider_map: dict[str, set[str]] | None = None,
    distgit_providers: dict[str, set[str]] | None = None,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
) -> PlannedPackage:
    bcond = parse_spec_bconds(
        spec_text, distro_macros=distro_macros, target_arches=target_arches
    )
    if parsed is None:
        parsed = _parse_spec_text(
            spec_text,
            f"{name}.spec",
            distro_macros=distro_macros,
            target_arches=target_arches,
        )
    chain = chain_sources or set()
    pmap = _merge_provider_maps(provider_map, distgit_providers)

    if not force_build and repo.satisfies_source(name):
        return PlannedPackage(
            name=name,
            action="skip",
            build_profile="full",
            rpmbuild_defines={},
            reason="repo satisfies source",
        )

    uncovered = _uncovered_plan_capabilities(parsed, bcond, repo, chain | {name}, [pmap])
    if uncovered:
        return PlannedPackage(
            name=name,
            action="block",
            build_profile="full",
            rpmbuild_defines={},
            reason=f"missing hard buildrequires: {', '.join(uncovered)}",
        )

    if force_build:
        return PlannedPackage(
            name=name,
            action="build",
            build_profile="full",
            rpmbuild_defines={},
            reason="forced root build",
        )

    return PlannedPackage(
        name=name,
        action="build",
        build_profile="full",
        rpmbuild_defines={},
        reason="full build required",
    )


def _slots_to_chain_spec(layers: list[list[PlannedPackage]]) -> str:
    stage_groups: list[list[str]] = []
    for layer in layers:
        names = [p.name for p in layer if p.action == "build"]
        if names:
            stage_groups.append(names)
    return " : ".join(" ".join(grp) for grp in stage_groups)


def _package_plan_dict(slot: PlannedPackage) -> dict[str, Any]:
    out: dict[str, Any] = {
        "build_profile": slot.build_profile,
        "rpmbuild_defines": dict(slot.rpmbuild_defines),
    }
    if slot.next_profile:
        out["next_profile"] = slot.next_profile
    return out


def _plan_all_slots(
    parsed_by_source: dict[str, Any],
    specs_by_pkg: dict[str, str],
    repo_index: RepoCapabilityIndex,
    *,
    force_set: set[str],
    scope_packages: set[str],
    repo_caps: set[str],
    distgit_providers: dict[str, set[str]] | None = None,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
) -> tuple[list[str], list[str], dict[str, PlannedPackage]]:
    selected_local: list[str] = []
    skipped_local: list[str] = []
    slots_local: dict[str, PlannedPackage] = {}
    provider_map = _merge_provider_maps(
        _provider_map_from_specs(parsed_by_source), distgit_providers
    )
    pending: list[str] = []
    for pkg in sorted(scope_packages):
        if pkg not in parsed_by_source:
            continue
        spec_text = specs_by_pkg.get(pkg) or ""
        if pkg not in force_set and repo_index.satisfies_source(pkg):
            slots_local[pkg] = PlannedPackage(
                name=pkg,
                action="skip",
                build_profile="full",
                rpmbuild_defines={},
                reason="repo satisfies source",
            )
            skipped_local.append(pkg)
            continue
        pending.append(pkg)

    # Only skip/build packages cover BRs. Blocked packages never do.
    coverable = set(skipped_local) | set(force_set)
    changed = True
    while changed:
        changed = False
        for pkg in list(pending):
            spec_text = specs_by_pkg.get(pkg) or ""
            slot = _plan_slots_for_package(
                pkg,
                spec_text,
                repo_index,
                parsed=parsed_by_source.get(pkg),
                force_build=pkg in force_set,
                repo_caps=repo_caps,
                chain_sources=coverable | {pkg},
                provider_map=provider_map,
                distgit_providers=None,
                distro_macros=distro_macros,
                target_arches=target_arches,
            )
            if slot.action == "block":
                continue
            slots_local[pkg] = slot
            pending.remove(pkg)
            coverable.add(pkg)
            if slot.action == "skip":
                skipped_local.append(pkg)
            else:
                selected_local.append(pkg)
            changed = True

    for pkg in pending:
        spec_text = specs_by_pkg.get(pkg) or ""
        slot = _plan_slots_for_package(
            pkg,
            spec_text,
            repo_index,
            parsed=parsed_by_source.get(pkg),
            force_build=pkg in force_set,
            repo_caps=repo_caps,
            chain_sources=coverable | {pkg},
            provider_map=provider_map,
            distgit_providers=None,
            distro_macros=distro_macros,
            target_arches=target_arches,
        )
        slots_local[pkg] = slot
        if slot.action == "skip":
            skipped_local.append(pkg)
        elif slot.action != "block":
            selected_local.append(pkg)
    return selected_local, skipped_local, slots_local


def _filter_unbuildable(
    selected: list[str],
    specs_by_pkg: dict[str, str],
    repo_index: RepoCapabilityIndex,
    force_set: set[str] | None = None,
    parsed_by_source: dict[str, Any] | None = None,
    distgit_providers: dict[str, set[str]] | None = None,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    force = force_set or set()
    chain = set(selected) | set(force)
    provider_map = _merge_provider_maps(
        _provider_map_from_specs(parsed_by_source or {}), distgit_providers
    )
    ok: list[str] = []
    plan_errors: list[str] = []
    for pkg in selected:
        if pkg in force:
            ok.append(pkg)
            continue
        spec_text = specs_by_pkg.get(pkg) or ""
        if not spec_text:
            ok.append(pkg)
            continue
        bcond = parse_spec_bconds(
            spec_text, distro_macros=distro_macros, target_arches=target_arches
        )
        parsed = parsed_by_source.get(pkg)
        if not parsed:
            ok.append(pkg)
            continue
        bad = _uncovered_plan_capabilities(parsed, bcond, repo_index, chain, [provider_map])
        if bad:
            plan_errors.append(f"{pkg}: missing build deps {', '.join(sorted(bad))}")
        else:
            ok.append(pkg)
    return ok, plan_errors


def _required_package_set(
    roots: list[str],
    selected: list[str],
    parsed_by_source: dict[str, Any],
    repo_index: RepoCapabilityIndex,
    distgit_providers: dict[str, set[str]] | None = None,
) -> set[str]:
    root_set = set(roots)
    known = set(parsed_by_source.keys())
    required = set(root_set)
    queue = list(root_set)
    provider_map = _merge_provider_maps(_provider_map_from_specs(parsed_by_source), distgit_providers)

    while queue:
        pkg = queue.pop()
        parsed = parsed_by_source.get(pkg)
        if not parsed:
            continue
        for dep in dependency_capabilities(parsed):
            provider_map_list = [provider_map]
            if repo_index.satisfies_requirement(dep):
                _missing_caps, provider_sources = _repo_transitive_miss_sources(
                    repo_index,
                    dep,
                    distgit_providers=distgit_providers,
                )
                for src in provider_sources:
                    if src in known and src not in required:
                        required.add(src)
                        queue.append(src)
                for miss in _missing_caps:
                    for src in sources_for_requirement(miss, repo_index, provider_maps=provider_map_list):
                        if src in known and src not in required:
                            required.add(src)
                            queue.append(src)
                continue
            for src in sources_for_requirement(dep, repo_index, provider_maps=provider_map_list):
                if src in known and src not in required:
                    required.add(src)
                    queue.append(src)
            for prov in provider_map.get(dep) or set():
                if prov in known and prov not in required:
                    required.add(prov)
                    queue.append(prov)
            _missing_caps, provider_sources = _repo_transitive_miss_sources(
                repo_index,
                dep,
                distgit_providers=distgit_providers,
            )
            for src in provider_sources:
                if src in known and src not in required:
                    required.add(src)
                    queue.append(src)
            for miss in _missing_caps:
                for src in sources_for_requirement(miss, repo_index, provider_maps=provider_map_list):
                    if src in known and src not in required:
                        required.add(src)
                        queue.append(src)
    return required


def _prune_unneeded_selected(
    roots: list[str],
    selected: list[str],
    parsed_by_source: dict[str, Any],
    repo_index: RepoCapabilityIndex,
    distgit_providers: dict[str, set[str]] | None = None,
) -> list[str]:
    required = _required_package_set(roots, selected, parsed_by_source, repo_index, distgit_providers)
    return [p for p in selected if p in required]


def _missing_distgit_sources_for_required(
    *,
    releasename: str,
    required: set[str],
    missing_distgit: set[str],
    needers: dict[str, set[str]],
    plan_errors: list[str],
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def on_path(src: str) -> bool:
        consumers = needers.get(src, set())
        if consumers and (consumers & required):
            return True
        for line in plan_errors:
            if not line.startswith(f"{src}: not in distgit at "):
                continue
            m = re.search(r"needed by ([^),]+)", line.strip())
            if not m:
                continue
            who = {w.strip() for w in m.group(1).split(",") if w.strip()}
            if who & required:
                return True
        if not consumers and src in required:
            return True
        return False

    for line in plan_errors:
        m = re.match(r"^(\S+): not in distgit at ", line.strip())
        if not m:
            continue
        src = m.group(1)
        if src in seen or not on_path(src):
            continue
        seen.add(src)
        out.append(line.strip())

    for src in sorted(missing_distgit):
        if src in seen or not on_path(src):
            continue
        who = ", ".join(sorted(needers.get(src, set()) & required)[:4])
        if not who:
            who = ", ".join(sorted(needers.get(src, set()))[:4])
        out.append(f"{src}: not in distgit at {releasename}/{src}/ (needed by {who})")
        seen.add(src)

    return out


def _collect_blocking_errors(
    *,
    roots: list[str],
    selected: list[str],
    required: set[str],
    releasename: str,
    specs_by_pkg: dict[str, str],
    parsed_by_source: dict[str, Any],
    slots_by_name: dict[str, PlannedPackage],
    repo_index: RepoCapabilityIndex,
    build_errs: list[str],
    plan_errors: list[str],
    missing_distgit: set[str],
    distgit_needers: dict[str, set[str]],
    distgit_providers: dict[str, set[str]] | None = None,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    blocked = sorted(
        pkg for pkg, slot in slots_by_name.items() if slot.action == "block" and pkg in required
    )

    def add(msg: str) -> None:
        m = (msg or "").strip()
        if not m or m in seen:
            return
        seen.add(m)
        out.append(m)

    for line in _missing_distgit_sources_for_required(
        releasename=releasename,
        required=required,
        missing_distgit=missing_distgit,
        needers=distgit_needers,
        plan_errors=plan_errors,
    ):
        add(line)

    distgit_srcs = {line.split(":", 1)[0].strip() for line in out if ": not in distgit at " in line}
    provider_map = _merge_provider_maps(_provider_map_from_specs(parsed_by_source), distgit_providers)
    import_caps: list[str] = []
    other_blocks: list[str] = []

    for pkg in blocked:
        slot = slots_by_name[pkg]
        still = slot.reason
        if still.startswith("missing hard buildrequires:"):
            br_part = still.split(":", 1)[-1]
            brs = [b.strip() for b in br_part.split(",") if b.strip()]
            mapped: set[str] = set()
            bare_brs: list[str] = []
            pkg_import: list[str] = []
            for br in brs:
                no_prov = br.endswith("(no distgit provider)")
                cap = br[: -len(" (no distgit provider)")].strip() if no_prov else br
                bare_brs.append(cap)
                if no_prov:
                    pkg_import.append(cap)
                    continue
                mapped.update(sources_for_requirement(cap, repo_index, provider_maps=[provider_map]))
            if mapped and mapped <= distgit_srcs and not pkg_import:
                continue
            if pkg_import:
                import_caps.extend(pkg_import)
                add(
                    f"{pkg}: missing hard buildrequires with no distgit provider: "
                    f"{', '.join(sorted(set(pkg_import)))}"
                )
            else:
                other_blocks.append(f"{pkg}: missing hard buildrequires: {', '.join(bare_brs)}")
            continue
        other_blocks.append(f"{pkg}: {slot.reason}")

    for msg in build_errs:
        pkg = (msg.split(":", 1)[0] or "").strip()
        if pkg and pkg not in required:
            continue
        if ": not in distgit at " in msg:
            continue
        add(msg)

    if import_caps or distgit_srcs:
        if roots and distgit_srcs:
            add(
                f"{roots[0]}: import into rpm-specfiles then retry: "
                f"{', '.join(sorted(distgit_srcs)[:12])}"
            )
        if import_caps:
            uniq = sorted(set(import_caps))
            add(
                f"{roots[0] if roots else releasename}: import into rpm-specfiles the package(s) that "
                f"Provide: {', '.join(uniq[:12])} then retry"
            )
        return out

    for msg in other_blocks:
        add(msg)
    if blocked and roots:
        add(
            f"{roots[0]}: required dependencies cannot be built: {', '.join(blocked[:12])}"
        )
    return out


def _build_layers(
    selected: list[str],
    parsed_by_source: dict[str, Any],
    slots_by_name: dict[str, PlannedPackage],
    repo_index: RepoCapabilityIndex,
    distgit_providers: dict[str, set[str]] | None = None,
) -> tuple[list[list[PlannedPackage]], bool, list[str]]:
    errors: list[str] = []
    names_set = set(selected)
    deps: dict[str, set[str]] = {n: set() for n in names_set}
    rdeps: dict[str, set[str]] = {n: set() for n in names_set}
    provider_map = _merge_provider_maps(_provider_map_from_specs(parsed_by_source), distgit_providers)

    for cons in selected:
        parsed = parsed_by_source.get(cons)
        if not parsed:
            continue
        for dep in dependency_capabilities(parsed):
            providers = provider_map.get(dep) or set()
            for prov in sorted(providers):
                if prov not in names_set or prov == cons:
                    continue
                deps[cons].add(prov)
                rdeps[prov].add(cons)
            if not repo_index.satisfies_requirement(dep):
                for src in sources_for_requirement(dep, repo_index, provider_maps=[provider_map]):
                    if src in names_set and src != cons:
                        deps[cons].add(src)
                        rdeps[src].add(cons)
            for miss in repo_index.transitive_unsatisfied(dep):
                for src in sources_for_requirement(miss, repo_index, provider_maps=[provider_map]):
                    if src in names_set and src != cons:
                        deps[cons].add(src)
                        rdeps[src].add(cons)
                for src in repo_index.distgit_sources_for_capability(miss):
                    if src in names_set and src != cons:
                        deps[cons].add(src)
                        rdeps[src].add(cons)
                for src in _providers_from_maps(miss, [distgit_providers or {}]):
                    if src in names_set and src != cons:
                        deps[cons].add(src)
                        rdeps[src].add(cons)

    layers_raw, cyc = _kahn_layers(names_set, deps, rdeps) if names_set else ([], False)
    if cyc:
        errors.append(
            "Warning: a cycle exists in distgit BuildRequires; some parallel layers may be conservative."
        )

    layers: list[list[PlannedPackage]] = []
    for layer in layers_raw:
        layers.append([slots_by_name[n] for n in layer if n in slots_by_name])
    return layers, cyc, errors


async def plan_automatic_chain(
    *,
    releasename: str,
    repo_branch: str,
    distgit_url: str,
    distgit_branch: str,
    root_packages: list[str],
    architectures: list[str],
    headers: dict[str, str],
) -> BuildPlanResult:
    errors: list[str] = []
    t0 = time.monotonic()
    roots = sorted({(r or "").strip() for r in root_packages if (r or "").strip()})
    if not roots:
        return BuildPlanResult(
            releasename=releasename,
            repo_branch=repo_branch,
            layers=[],
            chain_spec="",
            package_plans={},
            skipped=[],
            errors=["No package names in chain spec"],
        )

    gr = _github_owner_repo_from_git_url(distgit_url)
    if not gr:
        return BuildPlanResult(
            releasename=releasename,
            repo_branch=repo_branch,
            layers=[],
            chain_spec="",
            package_plans={},
            skipped=[],
            errors=[f"distgit_url is not a supported github.com URL: {distgit_url!r}"],
        )
    owner, distgit_repo = gr
    arches = {a.lower() for a in (architectures or []) if a} or None

    repo_index = await asyncio.to_thread(
        load_repo_capability_index,
        releasename,
        repo_branch,
        architectures,
    )

    specs_by_pkg, parsed_by_source, perr = await expand_chain_spec_closure_sparse(
        roots,
        releasename=releasename,
        owner=owner,
        repo=distgit_repo,
        distgit_branch=distgit_branch,
        headers=headers,
        repo_index=repo_index,
        target_arches=arches,
    )
    errors.extend(perr)

    distgit_providers: dict[str, set[str]] = {}
    distro_macros = default_distro_macros(releasename)

    # Provides index is the source of truth for capability -> distgit source.
    # Wait for it. No naming heuristics as fallback.
    try:
        provides_index = await get_distgit_provides_index_cached(
            owner=owner,
            repo=distgit_repo,
            distgit_branch=distgit_branch,
            releasename=releasename,
            headers=headers,
        )
        if provides_index is not None:
            # independent copy so later resolve waves dont mutate the warm cache
            distgit_providers = merge_provider_maps(distgit_providers_dict(provides_index))
            logger.info(
                "distgit provides seed %s/%s: packages=%s caps=%s",
                releasename,
                distgit_branch,
                len(provides_index.package_names),
                len([k for k in distgit_providers if k != "__lower__"]),
            )
        else:
            logger.warning(
                "distgit provides index empty for %s/%s",
                releasename,
                distgit_branch,
            )
    except Exception as exc:
        logger.warning("distgit provides index unavailable: %s", exc)

    specs_by_pkg, parsed_by_source, rt_err = await fetch_repo_transitive_extras_for_roots(
        specs_by_pkg,
        parsed_by_source,
        repo_index,
        roots,
        releasename=releasename,
        owner=owner,
        repo_name=distgit_repo,
        distgit_branch=distgit_branch,
        headers=headers,
        distgit_providers=distgit_providers,
        target_arches=arches,
    )
    errors.extend(rt_err)

    repo_caps = repo_index.all_caps
    force_set = set(roots)
    skipped: list[str] = []
    slots_by_name: dict[str, PlannedPackage] = {}
    selected: list[str] = []
    build_errs: list[str] = []
    missing_distgit: set[str] = set()
    distgit_needers: dict[str, set[str]] = {}
    for _round in range(32):
        parsed_before = len(parsed_by_source)
        required = await asyncio.to_thread(
            _required_package_set,
            roots,
            selected,
            parsed_by_source,
            repo_index,
            distgit_providers,
        )
        specs_by_pkg, parsed_by_source, br_err, round_missing = await expand_unsatisfied_build_dep_sources(
            specs_by_pkg,
            parsed_by_source,
            repo_index,
            releasename=releasename,
            owner=owner,
            repo_name=distgit_repo,
            distgit_branch=distgit_branch,
            headers=headers,
            distgit_providers=distgit_providers,
            scope_packages=required,
            target_arches=arches,
        )
        errors.extend(br_err)
        missing_distgit.update(round_missing)
        distgit_needers = await asyncio.to_thread(
            _distgit_source_needers,
            specs_by_pkg,
            parsed_by_source,
            repo_index,
            distgit_providers=distgit_providers,
            scope_packages=required | set(parsed_by_source.keys()),
            distro_macros=distro_macros,
            target_arches=arches,
        )

        selected, skipped, slots_by_name = await asyncio.to_thread(
            _plan_all_slots,
            parsed_by_source,
            specs_by_pkg,
            repo_index,
            force_set=force_set,
            scope_packages=required | force_set,
            repo_caps=repo_caps,
            distgit_providers=distgit_providers,
            distro_macros=distro_macros,
            target_arches=arches,
        )

        selected, round_errs = await asyncio.to_thread(
            _filter_unbuildable,
            selected,
            specs_by_pkg,
            repo_index,
            force_set,
            parsed_by_source,
            distgit_providers,
            distro_macros,
            arches,
        )
        build_errs = round_errs
        selected = await asyncio.to_thread(
            _prune_unneeded_selected,
            roots,
            selected,
            parsed_by_source,
            repo_index,
            distgit_providers,
        )
        if len(parsed_by_source) == parsed_before:
            break

    for pkg, slot in list(slots_by_name.items()):
        if slot.action == "block" and pkg in required:
            errors.append(f"{pkg}: {slot.reason}")

    required = await asyncio.to_thread(
        _required_package_set,
        roots,
        selected,
        parsed_by_source,
        repo_index,
        distgit_providers,
    )

    blocking_errors = await asyncio.to_thread(
        _collect_blocking_errors,
        roots=roots,
        selected=selected,
        required=required,
        releasename=releasename,
        specs_by_pkg=specs_by_pkg,
        parsed_by_source=parsed_by_source,
        slots_by_name=slots_by_name,
        repo_index=repo_index,
        build_errs=build_errs,
        plan_errors=errors,
        missing_distgit=missing_distgit,
        distgit_needers=distgit_needers,
        distgit_providers=distgit_providers,
    )
    errors.extend(build_errs)

    if not selected and roots:
        for rn in roots:
            if rn in parsed_by_source and rn not in skipped:
                spec_text = specs_by_pkg.get(rn, "")
                slot = _plan_slots_for_package(
                    rn,
                    spec_text,
                    repo_index,
                    force_build=True,
                    repo_caps=repo_caps,
                    distro_macros=distro_macros,
                    target_arches=arches,
                )
                slots_by_name[rn] = slot
                if slot.action == "build":
                    selected.append(rn)
            elif rn not in parsed_by_source:
                spec_text = specs_by_pkg.get(rn, "")
                if spec_text:
                    parsed_by_source[rn] = _parse_spec_text(
                        spec_text,
                        f"{rn}.spec",
                        distro_macros=distro_macros,
                        target_arches=arches,
                    )
                    slot = _plan_slots_for_package(
                        rn,
                        spec_text,
                        repo_index,
                        force_build=True,
                        repo_caps=repo_caps,
                        distro_macros=distro_macros,
                        target_arches=arches,
                    )
                    slots_by_name[rn] = slot
                    if slot.action == "build":
                        selected.append(rn)
        selected, more_errs = await asyncio.to_thread(
            _filter_unbuildable,
            selected,
            specs_by_pkg,
            repo_index,
            force_set,
            parsed_by_source,
            distgit_providers,
            distro_macros,
            arches,
        )
        errors.extend(more_errs)

    layers, cyc, layer_errs = await asyncio.to_thread(
        _build_layers,
        selected,
        parsed_by_source,
        slots_by_name,
        repo_index,
        distgit_providers,
    )
    errors.extend(layer_errs)

    package_plans = {
        name: _package_plan_dict(slot)
        for name, slot in slots_by_name.items()
        if slot.action == "build" and name in set(selected)
    }
    chain_spec = _slots_to_chain_spec(layers)

    logger.info(
        "build plan %s: roots=%s build=%s skip=%s layers=%s in %.1fs",
        releasename,
        len(roots),
        len(selected),
        len(skipped),
        len(layers),
        time.monotonic() - t0,
    )

    return BuildPlanResult(
        releasename=releasename,
        repo_branch=repo_branch,
        layers=layers,
        chain_spec=chain_spec,
        package_plans=package_plans,
        skipped=skipped,
        errors=errors,
        blocking_errors=blocking_errors,
        cycle_warning=cyc,
    )


def chain_spec_root_packages(chain_spec: str) -> list[str]:
    parts = re.split(r"[\s:]+", (chain_spec or "").strip())
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        name = (p or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


async def fetch_and_plan_automatic_chain(
    *,
    releasename: str,
    repo_branch: str,
    distgit_url: str,
    distgit_branch: str,
    root_packages: list[str],
    architectures: list[str],
    headers: dict[str, str],
) -> tuple[BuildPlanResult, list[str]]:
    plan = await plan_automatic_chain(
        releasename=releasename,
        repo_branch=repo_branch,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        root_packages=root_packages,
        architectures=architectures,
        headers=headers,
    )
    return plan, []
