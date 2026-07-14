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
    is_distgit_source_name,
    merge_provider_maps,
    requirement_check_capabilities,
    similar_sources_for_requirement,
    sources_for_requirement,
    sources_providing_requirement,
)
from oreon_build.core.spec_bcond import (
    default_distro_macros,
    parse_spec_bconds,
    solve_bootstrap_defines,
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
from oreon_build.services.repo_solver import MultiArchRepoSolver, get_repo_solver

logger = logging.getLogger(__name__)


def _merge_provider_maps(*maps: dict[str, set[str]] | None) -> dict[str, set[str]]:
    return merge_provider_maps(*maps)


def _chain_covers_requirement(
    requirement: str,
    repo: RepoCapabilityIndex,
    chain: set[str],
    provider_maps: list[dict[str, set[str]]],
    solver: MultiArchRepoSolver | None = None,
) -> bool:
    if solver is not None:
        result = solver.resolve([requirement])
        if result.ok:
            return True
        for miss in result.missing_provides or {requirement}:
            if not is_chain_plan_requirement(miss):
                continue
            if miss in chain:
                continue
            providers = sources_providing_requirement(
                miss, repo, provider_maps=provider_maps, exact=True
            )
            if providers & chain:
                continue
            return False
        return True
    if repo.satisfies_requirement(requirement):
        return True
    return any(
        s in chain
        for s in sources_for_requirement(
            requirement, repo, provider_maps=provider_maps
        )
    )


def _uncovered_plan_capabilities(
    parsed: Any,
    bcond: Any,
    repo: RepoCapabilityIndex,
    chain: set[str],
    provider_maps: list[dict[str, set[str]]] | None = None,
    solver: MultiArchRepoSolver | None = None,
) -> list[str]:
    pmap = provider_maps or []
    caps = [
        cap
        for cap in iter_plan_capabilities(parsed, bcond, repo)
        if is_chain_plan_requirement(cap)
    ]
    if not caps:
        return []

    uncovered: list[str] = []
    if solver is not None:
        result = solver.resolve(caps)
        if result.ok:
            return []
        for miss in sorted(result.missing_provides or set(caps)):
            if not is_chain_plan_requirement(miss):
                continue
            providers = sources_providing_requirement(miss, repo, provider_maps=pmap, exact=True)
            if providers & chain or miss in chain:
                continue
            if providers:
                uncovered.append(miss)
            else:
                uncovered.append(f"{miss} (no distgit provider)")
        return uncovered

    for cap in caps:
        if _chain_covers_requirement(cap, repo, chain, pmap):
            continue
        if repo.satisfies_requirement(cap):
            continue
        providers = sources_for_requirement(cap, repo, provider_maps=pmap, exact=True)
        if providers:
            uncovered.append(cap)
            continue
        if getattr(repo, "has_capability_ci", None) and repo.has_capability_ci(cap):
            uncovered.append(
                f"{cap} (no exact Provide in repo; case-different name exists, fix BR or add Provides)"
            )
            continue
        similar = similar_sources_for_requirement(cap, repo, provider_maps=pmap)
        if similar:
            uncovered.append(
                f"{cap} (no exact Provide; similar source(s): {', '.join(sorted(similar))})"
            )
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
    solver: MultiArchRepoSolver | None = None,
    solver_failed_pkgs: set[str] | None = None,
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

    # published packages forced for uninstallable provides always rebuild.
    # unpublished roots stay in force_set but must still pass BR coverage.
    if force_build and repo.satisfies_source(name):
        profile, toggled = solve_bootstrap_defines(bcond, repo)
        defines = dict(toggled) if toggled else {}
        if profile == "blocked":
            for key in list(bcond.defaults.keys()):
                if key.startswith("with_"):
                    defines[key] = "0"
            profile = "bootstrap"
        if defines or profile == "bootstrap":
            defines.setdefault("perl_bootstrap", "1")
            if any(
                "optional_test" in k or "extra_test" in k or "enables_" in k
                for k in defines
            ):
                defines["oreon"] = "0"
            profile = "bootstrap"
        else:
            profile = "full"
            defines = {}
        return PlannedPackage(
            name=name,
            action="build",
            build_profile=profile,
            rpmbuild_defines=defines,
            reason="forced rebuild for uninstallable provider",
        )

    if name in (solver_failed_pkgs or ()):
        return PlannedPackage(
            name=name,
            action="block",
            build_profile="full",
            rpmbuild_defines={},
            reason="dnf cannot install hard buildrequires under current repos/overlay",
        )

    uncovered = _uncovered_plan_capabilities(
        parsed, bcond, repo, chain | {name}, [pmap], solver=solver
    )
    if uncovered:
        return PlannedPackage(
            name=name,
            action="block",
            build_profile="full",
            rpmbuild_defines={},
            reason=f"missing hard buildrequires: {' | '.join(uncovered)}",
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


def _valid_overlay_capability(cap: str) -> bool:
    c = (cap or "").strip()
    if not c or len(c) > 220 or "%" in c:
        return False
    if any(ch in c for ch in ("'", '"', "`", "$", ";", "|", "{", "}", "\\", "\n", "\t")):
        return False
    if c.startswith(("(", "=", "<", ">", "!")):
        return False
    if not re.match(r"^[A-Za-z0-9_./+]", c):
        return False
    if c.lower() in {
        "printf",
        "echo",
        "true",
        "false",
        "test",
        "then",
        "else",
        "fi",
        "grep",
        "sed",
        "awk",
    }:
        return False
    return True


def _overlay_provides_for_sources(
    names: set[str],
    parsed_by_source: dict[str, Any],
    distgit_providers: dict[str, set[str]] | None = None,
) -> dict[str, set[str]]:
    by_src: dict[str, set[str]] = {}
    if distgit_providers and names:
        # reverse index only for requested names (full scan was death with big overlays)
        want = set(names)
        for cap, srcs in distgit_providers.items():
            if cap == "__lower__" or not isinstance(srcs, (set, frozenset, list, tuple)):
                continue
            hit = want.intersection(srcs) if not isinstance(srcs, set) else (want & srcs)
            if not hit:
                continue
            for src in hit:
                by_src.setdefault(src, set()).add(cap)
    out: dict[str, set[str]] = {}
    for name in names:
        caps = set(by_src.get(name, set()))
        parsed = parsed_by_source.get(name)
        if parsed:
            caps.update(getattr(parsed, "provided_binary_names", set()) or set())
        caps.add(name)
        out[name] = {c for c in caps if _valid_overlay_capability(c)}
    return out


def _provider_rebuild_force_sources(
    packages: set[str],
    parsed_by_source: dict[str, Any],
    specs_by_pkg: dict[str, str],
    repo_index: RepoCapabilityIndex,
    distgit_providers: dict[str, set[str]] | None = None,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
    solver: MultiArchRepoSolver | None = None,
    *,
    walk_provider_path: bool | None = None,
) -> tuple[set[str], bool, set[str], set[str]]:
    """Sources already in repo that must rebuild for unmet / uninstallable BRs.

    Returns (force_sources, every_pkg_ok, pkgs_with_failed_resolve, fetch_sources).
    fetch_sources are distgit providers not yet published (expand must fetch them).
    """
    from oreon_build.core.spec_bcond import active_build_brs, default_build_defines, parse_spec_bconds

    out: set[str] = set()
    fetch: set[str] = set()
    bad_pkgs: set[str] = set()
    provider_map = _merge_provider_maps(
        _provider_map_from_specs(parsed_by_source), distgit_providers
    )
    all_ok = True
    br_fp_cache: dict[tuple[str, ...], Any] = {}
    pkgs_solved = 0
    t0 = time.monotonic()
    do_walk = (solver is None) if walk_provider_path is None else walk_provider_path

    def _add_force_src(src: str) -> None:
        if not src or not is_distgit_source_name(src):
            return
        if repo_index.satisfies_source(src):
            out.add(src)
        else:
            fetch.add(src)

    def _map_miss(miss: str) -> None:
        if not is_chain_plan_requirement(miss):
            return
        for src in sources_providing_requirement(
            miss, repo_index, provider_maps=[provider_map], exact=True
        ):
            _add_force_src(src)

    for pkg in packages:
        spec_text = specs_by_pkg.get(pkg) or ""
        if not spec_text and pkg not in parsed_by_source:
            continue
        bcond = parse_spec_bconds(
            spec_text, distro_macros=distro_macros, target_arches=target_arches
        )
        leaf_misses: list[str] = []
        leaf_ok: list[str] = []
        for br in active_build_brs(bcond, default_build_defines(bcond)):
            if not is_chain_plan_requirement(br):
                continue
            if repo_index.satisfies_requirement(br):
                if solver is not None:
                    leaf_ok.append(br)
                elif do_walk:
                    leaf_misses.extend(repo_index.missing_caps_on_provider_path(br))
            else:
                leaf_misses.append(br)

        misses: list[str] = list(leaf_misses)
        broken_bins: set[str] = set()
        if solver is not None and leaf_ok:
            key = tuple(sorted(set(leaf_ok)))
            result = br_fp_cache.get(key)
            if result is None:
                result = solver.resolve(key)
                br_fp_cache[key] = result
                pkgs_solved += 1
            if not result.ok:
                all_ok = False
                bad_pkgs.add(pkg)
                misses.extend(result.missing_provides or key)
                broken_bins |= set(result.broken_binaries or ())

        for miss in misses:
            _map_miss(miss)

        for bin_name in broken_bins:
            src = repo_index.binary_source.get(bin_name) or ""
            if not src and repo_index.satisfies_source(bin_name):
                src = bin_name
            if not src:
                # binary name often matches the source for noarch perl modules
                for hit in sources_providing_requirement(
                    bin_name, repo_index, provider_maps=[provider_map], exact=True
                ):
                    _add_force_src(hit)
                if repo_index.satisfies_source(bin_name) or bin_name in parsed_by_source:
                    _add_force_src(bin_name)
                continue
            _add_force_src(src)

    if solver is not None:
        logger.info(
            "solver force: pkgs=%s unique_br_sets=%s solved=%s ok=%s force=%s fetch=%s bad=%s in %.1fs",
            len(packages),
            len(br_fp_cache),
            pkgs_solved,
            all_ok,
            len(out),
            len(fetch),
            len(bad_pkgs),
            time.monotonic() - t0,
        )
    return out, all_ok if solver is not None else True, bad_pkgs, fetch


_version_rebuild_force_sources = _provider_rebuild_force_sources


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
    solver: MultiArchRepoSolver | None = None,
    solver_failed_pkgs: set[str] | None = None,
) -> tuple[list[str], list[str], dict[str, PlannedPackage]]:
    selected_local: list[str] = []
    skipped_local: list[str] = []
    slots_local: dict[str, PlannedPackage] = {}
    provider_map = _merge_provider_maps(
        _provider_map_from_specs(parsed_by_source), distgit_providers
    )
    failed = solver_failed_pkgs or set()
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

    # force packages first so dependents only cover after force is actually selected
    pending.sort(key=lambda p: (0 if p in force_set else 1, p))

    # Only skip/build packages cover BRs. Blocked packages never do.
    # force_set is NOT coverable until selected as build.
    coverable = set(skipped_local)
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
                solver=solver,
                solver_failed_pkgs=failed,
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
            solver=solver,
            solver_failed_pkgs=failed,
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
    solver: MultiArchRepoSolver | None = None,
) -> tuple[list[str], list[str]]:
    force = force_set or set()
    # only selected packages cover. force-only names that never got selected
    # must not virtual-cover dependents (that was letting ExtUtils queue without CMR).
    chain = set(selected)
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
        parsed = (parsed_by_source or {}).get(pkg)
        if not parsed:
            ok.append(pkg)
            continue
        bad = _uncovered_plan_capabilities(
            parsed, bcond, repo_index, chain, [provider_map], solver=solver
        )
        if bad:
            plan_errors.append(f"{pkg}: missing build deps {', '.join(sorted(bad))}")
        else:
            ok.append(pkg)
    return ok, plan_errors


def _build_layers(
    selected: list[str],
    parsed_by_source: dict[str, Any],
    slots_by_name: dict[str, PlannedPackage],
    repo_index: RepoCapabilityIndex,
    distgit_providers: dict[str, set[str]] | None = None,
    force_set: set[str] | None = None,
) -> tuple[list[list[PlannedPackage]], bool, list[str]]:
    errors: list[str] = []
    names_set = set(selected)
    force = {f for f in (force_set or ()) if f in names_set}
    deps: dict[str, set[str]] = {n: set() for n in names_set}
    rdeps: dict[str, set[str]] = {n: set() for n in names_set}
    provider_map = _merge_provider_maps(_provider_map_from_specs(parsed_by_source), distgit_providers)

    def _edge(cons: str, prov: str) -> None:
        if prov not in names_set or prov == cons:
            return
        deps[cons].add(prov)
        rdeps[prov].add(cons)

    for cons in selected:
        parsed = parsed_by_source.get(cons)
        if not parsed:
            continue
        for dep in dependency_capabilities(parsed):
            providers: set[str] = set()
            for cap in requirement_check_capabilities(dep):
                providers.update(_providers_from_maps(cap, [provider_map], exact=True))
            for prov in sorted(providers):
                _edge(cons, prov)
            for src in sources_providing_requirement(
                dep, repo_index, provider_maps=[provider_map], exact=True
            ):
                _edge(cons, src)
            if not repo_index.satisfies_requirement(dep):
                for src in sources_for_requirement(dep, repo_index, provider_maps=[provider_map]):
                    _edge(cons, src)
            for miss in repo_index.missing_caps_on_provider_path(dep):
                for src in sources_providing_requirement(
                    miss, repo_index, provider_maps=[provider_map], exact=True
                ):
                    _edge(cons, src)
                for src in sources_for_requirement(miss, repo_index, provider_maps=[provider_map]):
                    _edge(cons, src)
                for src in repo_index.owner_sources_for_capability(miss):
                    _edge(cons, src)
                for src in _providers_from_maps(miss, [distgit_providers or {}]):
                    _edge(cons, src)

    # force rebuilds for uninstallable provides must finish before dependents
    # that only look leaf-ok under a fake overlay
    for cons in names_set - force:
        parsed = parsed_by_source.get(cons)
        if not parsed:
            continue
        for dep in dependency_capabilities(parsed):
            walk = [dep]
            if repo_index.satisfies_requirement(dep):
                walk.extend(repo_index.missing_caps_on_provider_path(dep))
            for miss in walk:
                for src in sources_providing_requirement(
                    miss, repo_index, provider_maps=[provider_map], exact=True
                ):
                    if src in force:
                        _edge(cons, src)

    layers_raw, cyc = _kahn_layers(names_set, deps, rdeps) if names_set else ([], False)
    if cyc:
        errors.append(
            "Warning: a cycle exists in distgit BuildRequires; some parallel layers may be conservative."
        )

    layers: list[list[PlannedPackage]] = []
    for layer in layers_raw:
        layers.append([slots_by_name[n] for n in layer if n in slots_by_name])
    return layers, cyc, errors


def _required_package_set(
    roots: list[str],
    selected: list[str],
    parsed_by_source: dict[str, Any],
    repo_index: RepoCapabilityIndex,
    distgit_providers: dict[str, set[str]] | None = None,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
    specs_by_pkg: dict[str, str] | None = None,
) -> set[str]:
    """roots + selected + unmet dep providers already fetched.

    Pathwalk only for packages we are actually building (roots/selected). That
    finds uninstallable published deps without sucking every parsed package into
    a distro-wide BFS.
    """
    root_set = {(r or "").strip() for r in roots if (r or "").strip()}
    selected_set = {(s or "").strip() for s in selected if (s or "").strip()}
    known = set(parsed_by_source.keys())
    required = set(root_set) | set(selected_set)
    queue = list(required)
    provider_map = _merge_provider_maps(
        _provider_map_from_specs(parsed_by_source), distgit_providers
    )
    build_focus = set(root_set) | set(selected_set)

    while queue:
        pkg = queue.pop()
        parsed = parsed_by_source.get(pkg)
        if not parsed:
            continue
        walk = pkg in root_set
        for dep in dependency_capabilities(parsed):
            dep = (dep or "").strip()
            if not dep or "%" in dep:
                continue
            if not walk and not is_chain_plan_requirement(dep):
                continue
            if repo_index.satisfies_requirement(dep):
                if not walk:
                    continue
                for miss in repo_index.missing_caps_on_provider_path(dep):
                    if not is_chain_plan_requirement(miss) and miss == dep:
                        continue
                    for src in sources_for_requirement(
                        miss, repo_index, provider_maps=[provider_map], exact=True
                    ):
                        if src in known and src not in required:
                            required.add(src)
                            queue.append(src)
                continue
            for src in sources_for_requirement(
                dep, repo_index, provider_maps=[provider_map], exact=True
            ):
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
    required = _required_package_set(
        roots,
        selected,
        parsed_by_source,
        repo_index,
        distgit_providers,
    )
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
    missing_lines = _missing_distgit_sources_for_required(
        releasename=releasename,
        required=required,
        missing_distgit=missing_distgit,
        needers=distgit_needers,
        plan_errors=plan_errors,
    )
    imports: list[str] = []
    seen: set[str] = set()

    def add_import(name: str) -> None:
        n = (name or "").strip()
        if not n or n in seen:
            return
        seen.add(n)
        imports.append(n)

    for line in missing_lines:
        pkg = line.split(":", 1)[0].strip()
        add_import(pkg)

    blocked = sorted(
        pkg for pkg, slot in slots_by_name.items() if slot.action == "block" and pkg in required
    )
    distgit_srcs = set(imports)
    provider_map = _merge_provider_maps(_provider_map_from_specs(parsed_by_source), distgit_providers)

    for pkg in blocked:
        slot = slots_by_name[pkg]
        still = slot.reason
        if not still.startswith("missing hard buildrequires:"):
            continue
        br_part = still.split(":", 1)[-1]
        brs = [b.strip() for b in br_part.split(" | ") if b.strip()]
        mapped: set[str] = set()
        had_unresolved_import = False
        for br in brs:
            no_prov = (
                "(no distgit provider)" in br
                or "(no exact Provide" in br
                or "case-different name exists" in br
            )
            if br.endswith("(no distgit provider)"):
                cap = br[: -len(" (no distgit provider)")].strip()
            elif "(no exact Provide" in br:
                cap = br.split(" (no exact Provide", 1)[0].strip()
            elif "case-different name exists" in br:
                cap = br.split(" (no exact Provide", 1)[0].strip()
            else:
                cap = br
            if no_prov:
                had_unresolved_import = True
                resolved = sources_for_requirement(
                    cap, repo_index, provider_maps=[provider_map], exact=True
                )
                if resolved:
                    for src in sorted(resolved):
                        add_import(src)
                else:
                    if cap.startswith("/") and "/" in cap[1:]:
                        base = cap.rsplit("/", 1)[-1]
                        if base and is_distgit_source_name(base):
                            add_import(base)
                            continue
                    add_import(cap)
                continue
            mapped.update(
                sources_for_requirement(cap, repo_index, provider_maps=[provider_map], exact=True)
            )
        if mapped and mapped <= distgit_srcs and not had_unresolved_import:
            continue

    for msg in build_errs:
        pkg = (msg.split(":", 1)[0] or "").strip()
        if pkg and pkg not in required:
            continue
        if ": not in distgit at " in msg:
            continue
        # keep rare non-import blockers as literal lines
        if pkg and pkg not in seen:
            add_import(pkg)

    return imports


async def plan_automatic_chain(
    *,
    releasename: str,
    repo_branch: str,
    distgit_url: str,
    distgit_branch: str,
    root_packages: list[str],
    architectures: list[str],
    headers: dict[str, str],
    capability_extra_branches: list[str] | None = None,
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

    extra_branches = [b for b in (capability_extra_branches or []) if (b or "").strip()]
    repo_index = await asyncio.to_thread(
        lambda: load_repo_capability_index(
            releasename,
            repo_branch,
            architectures,
            extra_branches=extra_branches or None,
        )
    )
    solver: MultiArchRepoSolver | None = None
    try:
        solver = await asyncio.to_thread(
            get_repo_solver,
            releasename,
            repo_branch,
            list(architectures or []) or ["x86_64"],
            extra_branches=extra_branches or None,
        )
        await asyncio.to_thread(solver.set_overlay, {})
        await asyncio.to_thread(solver.warm)
        logger.info(
            "repo dnf solver ready %s/%s arches=%s",
            releasename,
            "+".join([repo_branch, *(extra_branches or [])]),
            ",".join(sorted((a or "").strip() for a in (architectures or []) if a)) or "x86_64",
        )
    except Exception as exc:
        logger.warning("repo dnf solver unavailable, falling back to leaf index: %s", exc)
        solver = None

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
        solver=None,
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
    pending_fetch: set[str] = set()
    for _round in range(32):
        parsed_before = len(parsed_by_source)
        force_before = frozenset(force_set)
        fetch_before = frozenset(pending_fetch)
        required = await asyncio.to_thread(
            _required_package_set,
            roots,
            selected,
            parsed_by_source,
            repo_index,
            distgit_providers,
            distro_macros,
            arches,
            specs_by_pkg,
        )
        logger.info(
            "plan round parsed=%s required=%s force=%s selected=%s pending_fetch=%s",
            len(parsed_by_source),
            len(required),
            len(force_set),
            len(selected),
            len(pending_fetch),
        )
        # only expand builddeps for packages we may build. expanding skippable
        # published packages fans the whole distro into the chain.
        expand_scope = {
            p
            for p in (required | force_set)
            if p in force_set or p in roots or not repo_index.satisfies_source(p)
        } | set(pending_fetch)
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
            scope_packages=expand_scope,
            target_arches=arches,
            solver=None,
        )
        errors.extend(br_err)
        missing_distgit.update(round_missing)
        pending_fetch -= set(parsed_by_source.keys())
        distgit_needers = await asyncio.to_thread(
            _distgit_source_needers,
            specs_by_pkg,
            parsed_by_source,
            repo_index,
            distgit_providers=distgit_providers,
            scope_packages=required | force_set,
            distro_macros=distro_macros,
            target_arches=arches,
        )

        # leaf/EVR force first (no depsolve)
        force_scope = required | force_set
        leaf_force, _, _, leaf_fetch = await asyncio.to_thread(
            _provider_rebuild_force_sources,
            force_scope,
            parsed_by_source,
            specs_by_pkg,
            repo_index,
            distgit_providers,
            distro_macros,
            arches,
            None,
            walk_provider_path=solver is None,
        )
        force_set |= leaf_force
        pending_fetch |= leaf_fetch - set(parsed_by_source.keys())

        install_ok = True
        solver_failed: set[str] = set()
        if solver is not None:
            # only depsolve packages we may actually build (not every satisfied skip)
            for _solv_round in range(4):
                build_pkgs = {
                    p
                    for p in (required | force_set)
                    if p in force_set
                    or p in roots
                    or not repo_index.satisfies_source(p)
                }
                if not build_pkgs:
                    install_ok = True
                    solver_failed = set()
                    break
                overlay_names = {
                    n
                    for n in (set(force_set) | set(selected) | set(roots))
                    if n in parsed_by_source or repo_index.satisfies_source(n) or n in roots
                }
                overlay = _overlay_provides_for_sources(
                    overlay_names, parsed_by_source, distgit_providers
                )
                await asyncio.to_thread(solver.set_overlay, overlay)
                before = set(force_set)
                added, install_ok, solver_failed, more_fetch = await asyncio.to_thread(
                    _provider_rebuild_force_sources,
                    build_pkgs,
                    parsed_by_source,
                    specs_by_pkg,
                    repo_index,
                    distgit_providers,
                    distro_macros,
                    arches,
                    solver,
                )
                pending_fetch |= more_fetch - set(parsed_by_source.keys())
                new_pub = added - before
                force_set |= added
                if install_ok or not new_pub:
                    break

        # force already depsolved build pkgs. slots must not re-depsolve.
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
            solver=None,
            solver_failed_pkgs=solver_failed,
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
            None,
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
        if (
            len(parsed_by_source) == parsed_before
            and force_before == frozenset(force_set)
            and fetch_before == frozenset(pending_fetch)
        ):
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
        distro_macros,
        arches,
        specs_by_pkg,
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
                    solver=solver,
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
                        solver=solver,
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
            solver,
        )
        errors.extend(more_errs)

    layers, cyc, layer_errs = await asyncio.to_thread(
        _build_layers,
        selected,
        parsed_by_source,
        slots_by_name,
        repo_index,
        distgit_providers,
        force_set,
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
    capability_extra_branches: list[str] | None = None,
) -> tuple[BuildPlanResult, list[str]]:
    plan = await plan_automatic_chain(
        releasename=releasename,
        repo_branch=repo_branch,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        root_packages=root_packages,
        architectures=architectures,
        headers=headers,
        capability_extra_branches=capability_extra_branches,
    )
    return plan, []
