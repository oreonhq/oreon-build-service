from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from oreon_build.core.spec_bcond import parse_spec_bconds, solve_bootstrap_defines
from oreon_build.services.distgit_order import (
    _github_owner_repo_from_git_url,
    _kahn_layers,
    _parse_spec_text,
    _provider_map_from_specs,
    expand_chain_spec_closure_sparse,
)
from oreon_build.services.repo_capability_index import RepoCapabilityIndex, load_repo_capability_index

logger = logging.getLogger(__name__)


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
    cycle_warning: bool = False


def _plan_slots_for_package(
    name: str,
    spec_text: str,
    repo: RepoCapabilityIndex,
    *,
    force_build: bool,
) -> PlannedPackage:
    bcond = parse_spec_bconds(spec_text)
    profile, defines = solve_bootstrap_defines(bcond, repo.provides | repo.binary_names | repo.source_names)

    if not force_build and profile == "full" and repo.satisfies_source(name):
        deps = bcond.unconditional_brs | bcond.unconditional_reqs | {
            br for brs in bcond.conditional_brs.values() for br in brs
        }
        if all(repo.has_capability(dep) for dep in deps):
            return PlannedPackage(
                name=name,
                action="skip",
                build_profile="full",
                rpmbuild_defines={},
                reason="repo satisfies source and dependencies",
            )

    if profile == "blocked":
        return PlannedPackage(
            name=name,
            action="build",
            build_profile="full",
            rpmbuild_defines={},
            reason="missing hard buildrequires; chain will try full build",
        )

    if profile == "bootstrap" and defines:
        return PlannedPackage(
            name=name,
            action="build",
            build_profile="bootstrap",
            rpmbuild_defines=defines,
            next_profile="full",
            reason="bootstrap pass with reduced bconds",
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
    owner, repo = gr

    closure_coro = expand_chain_spec_closure_sparse(
        roots,
        releasename=releasename,
        owner=owner,
        repo=repo,
        distgit_branch=distgit_branch,
        headers=headers,
    )
    repo_coro = asyncio.to_thread(
        load_repo_capability_index,
        releasename,
        repo_branch,
        architectures,
    )
    (specs_by_pkg, parsed_by_source, perr), repo = await asyncio.gather(closure_coro, repo_coro)
    errors.extend(perr)

    force_set = set(roots)
    selected: list[str] = []
    skipped: list[str] = []
    slots_by_name: dict[str, PlannedPackage] = {}

    for pkg in sorted(parsed_by_source.keys()):
        spec_text = specs_by_pkg.get(pkg) or ""
        slot = _plan_slots_for_package(
            pkg,
            spec_text,
            repo,
            force_build=pkg in force_set,
        )
        slots_by_name[pkg] = slot
        if slot.action == "skip":
            skipped.append(pkg)
        else:
            selected.append(pkg)

    if not selected and roots:
        for rn in roots:
            if rn in parsed_by_source and rn not in skipped:
                spec_text = specs_by_pkg.get(rn, "")
                slot = _plan_slots_for_package(rn, spec_text, repo, force_build=True)
                slots_by_name[rn] = slot
                if slot.action != "skip":
                    selected.append(rn)
            elif rn not in parsed_by_source:
                spec_text = specs_by_pkg.get(rn, "")
                if spec_text:
                    parsed_by_source[rn] = _parse_spec_text(spec_text, f"{rn}.spec")
                    slot = _plan_slots_for_package(rn, spec_text, repo, force_build=True)
                    slots_by_name[rn] = slot
                    if slot.action != "skip":
                        selected.append(rn)

    names_set = set(selected)
    deps: dict[str, set[str]] = {n: set() for n in names_set}
    rdeps: dict[str, set[str]] = {n: set() for n in names_set}
    provider_map = _provider_map_from_specs(parsed_by_source)

    for cons in selected:
        parsed = parsed_by_source.get(cons)
        if not parsed:
            continue
        for dep in parsed.dependency_names():
            providers = provider_map.get(dep) or set()
            for prov in sorted(providers):
                if prov not in names_set or prov == cons:
                    continue
                deps[cons].add(prov)
                rdeps[prov].add(cons)

    layers_raw, cyc = _kahn_layers(names_set, deps, rdeps) if names_set else ([], False)
    if cyc:
        errors.append(
            "Warning: a cycle exists in distgit BuildRequires; some parallel layers may be conservative."
        )

    layers: list[list[PlannedPackage]] = []
    for layer in layers_raw:
        layers.append([slots_by_name[n] for n in layer if n in slots_by_name])

    package_plans = {
        name: _package_plan_dict(slot)
        for name, slot in slots_by_name.items()
        if slot.action == "build"
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
