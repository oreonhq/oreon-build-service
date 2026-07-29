"""Orchestrate three-oracle dependency planning."""

from __future__ import annotations

import asyncio
import logging
import time

from oreon_build.core.rpm_requires import merge_provider_maps
from oreon_build.core.spec_bcond import default_distro_macros
from oreon_build.services.dep_plan.errors import root_coverage_errors
from oreon_build.services.dep_plan.expand import expand_fixed_point
from oreon_build.services.dep_plan.install_oracle import InstallOracle
from oreon_build.services.dep_plan.order import (
    build_order_edges,
    chain_spec_from_layers,
    layers_to_planned,
    package_plan_dict,
    topo_layers,
)
from oreon_build.services.dep_plan.types import BuildPlanResult, PlannedPackage
from oreon_build.services.distgit_order import (
    _github_owner_repo_from_git_url,
    distgit_providers_dict,
    expand_chain_spec_closure_sparse,
    get_distgit_provides_index_cached,
)
from oreon_build.services.repo_solver import get_repo_solver

logger = logging.getLogger(__name__)


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
    distro_macros = default_distro_macros(releasename)

    provides_task = asyncio.create_task(
        get_distgit_provides_index_cached(
            owner=owner,
            repo=distgit_repo,
            distgit_branch=distgit_branch,
            releasename=releasename,
            headers=headers,
        )
    )

    try:
        solver = await asyncio.to_thread(
            get_repo_solver,
            releasename,
            repo_branch,
            list(architectures or []) or ["x86_64"],
            extra_branches=extra_branches or None,
        )
        await asyncio.to_thread(solver.set_overlay, {})
        logger.info(
            "dep_plan solv ready %s/%s arches=%s",
            releasename,
            "+".join([repo_branch, *(extra_branches or [])]),
            ",".join(sorted((a or "").strip() for a in (architectures or []) if a))
            or "x86_64",
        )
    except Exception as exc:
        logger.exception("dep_plan solv unavailable")
        return BuildPlanResult(
            releasename=releasename,
            repo_branch=repo_branch,
            layers=[],
            chain_spec="",
            package_plans={},
            skipped=[],
            errors=[f"libsolv required for dependency planning: {exc}"],
        )

    install = InstallOracle(solver)
    repo_index = await asyncio.to_thread(solver.capability_index)
    logger.info(
        "dep_plan solv capability view sources=%s binaries=%s provides=%s",
        len(repo_index.source_names_any or repo_index.source_names),
        len(repo_index.binary_names_any or repo_index.binary_names),
        len(repo_index.provides),
    )

    provider_map: dict[str, set[str]] = {}
    package_names: set[str] = set()
    try:
        provides_index = await provides_task
        if provides_index is not None:
            provider_map = merge_provider_maps(distgit_providers_dict(provides_index))
            package_names = set(provides_index.package_names)
            logger.info(
                "dep_plan provides map %s/%s: packages=%s caps=%s",
                releasename,
                distgit_branch,
                len(package_names),
                len([k for k in provider_map if k != "__lower__"]),
            )
    except Exception as exc:
        logger.warning("dep_plan provides index unavailable: %s", exc)

    specs_by_pkg, parsed_by_source, sparse_err = await expand_chain_spec_closure_sparse(
        roots,
        releasename=releasename,
        owner=owner,
        repo=distgit_repo,
        distgit_branch=distgit_branch,
        headers=headers,
        repo_index=repo_index,
        target_arches=arches,
        distgit_providers=provider_map or None,
    )
    errors = list(sparse_err)

    nodes, provider_edges, path_errors, import_caps, expand_errs = await expand_fixed_point(
        roots=roots,
        specs_by_pkg=specs_by_pkg,
        parsed_by_source=parsed_by_source,
        repo_index=repo_index,
        install=install,
        provider_map=provider_map,
        package_names=package_names,
        releasename=releasename,
        owner=owner,
        repo=distgit_repo,
        distgit_branch=distgit_branch,
        headers=headers,
        architectures=list(architectures or []),
        distro_macros=distro_macros,
    )
    errors.extend(expand_errs)
    errors = list(dict.fromkeys(path_errors + errors))

    skipped = sorted(n for n, node in nodes.items() if node.action == "skip")
    build_names = {n for n, node in nodes.items() if node.action == "build"}

    # only keep builds on the required path from roots (providers of builds + roots)
    required = set(roots)
    changed = True
    while changed:
        changed = False
        for cons in list(required):
            if cons not in build_names and cons not in skipped:
                if cons in nodes and nodes[cons].action == "build":
                    required.add(cons)
                    changed = True
            for p in provider_edges.get(cons, set()):
                if p in build_names and p not in required:
                    required.add(p)
                    changed = True
    # include all build nodes that are ancestors of roots via reverse walk
    rdeps: dict[str, set[str]] = {n: set() for n in build_names}
    for cons, provs in provider_edges.items():
        if cons not in build_names:
            continue
        for p in provs:
            if p in build_names:
                rdeps.setdefault(p, set()).add(cons)
    keep = set()
    stack = [r for r in roots if r in build_names]
    while stack:
        cur = stack.pop()
        if cur in keep:
            continue
        keep.add(cur)
        for p in provider_edges.get(cur, set()):
            if p in build_names and p not in keep:
                stack.append(p)
    for n in list(build_names):
        if n not in keep and n not in roots:
            nodes[n].action = "skip"
            nodes[n].reason = "not on root dependency path"
            skipped.append(n)

    build_names = {n for n, node in nodes.items() if node.action == "build"}
    deps = build_order_edges(nodes, provider_edges=provider_edges)
    # restrict deps to build_names
    deps = {n: {p for p in deps.get(n, set()) if p in build_names} for n in build_names}
    layer_names, cycle = topo_layers(build_names, deps)
    layers = layers_to_planned(layer_names, nodes)

    package_plans = {
        n: package_plan_dict(nodes[n])
        for n in build_names
    }
    chain_spec = chain_spec_from_layers(layers)

    coverage = root_coverage_errors(roots, nodes, path_errors)
    if coverage:
        # surface as plan errors; trigger_bg fails on uncovered roots
        for e in coverage:
            if e not in errors:
                errors.insert(0, e)

    blocking_errors = list(import_caps)

    # ensure blocked roots show up in errors
    for r in roots:
        node = nodes.get(r)
        if node is not None and node.action == "block":
            msg = f"{r}: {node.reason}"
            if msg not in errors:
                errors.insert(0, msg)

    logger.info(
        "dep_plan %s: roots=%s build=%s skip=%s layers=%s imports=%s in %.1fs",
        releasename,
        len(roots),
        len(package_plans),
        len(skipped),
        len(layers),
        len(blocking_errors),
        time.monotonic() - t0,
    )

    return BuildPlanResult(
        releasename=releasename,
        repo_branch=repo_branch,
        layers=layers,
        chain_spec=chain_spec,
        package_plans=package_plans,
        skipped=sorted(set(skipped)),
        errors=errors,
        blocking_errors=blocking_errors,
        cycle_warning=cycle,
    )


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
