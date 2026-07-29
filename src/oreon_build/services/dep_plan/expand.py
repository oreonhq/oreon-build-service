"""Fixed-point expand: BR eval → solv → map misses → fetch sources → repeat."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from concurrent.futures import as_completed
from pathlib import Path
from typing import Any

import httpx

from oreon_build.core.rpm_requires import is_distgit_source_name
from oreon_build.core.spec_bcond import (
    default_build_defines,
    default_distro_macros,
    parse_spec_bconds,
    solve_bootstrap_defines,
)
from oreon_build.services.dep_plan.br_oracle import evaluate_many_brs
from oreon_build.services.dep_plan.errors import classify_misses, dedupe_keep_order
from oreon_build.services.dep_plan.install_oracle import InstallOracle, overlay_from_nodes
from oreon_build.services.dep_plan.providers import filter_plan_brs, providers_for_requirement
from oreon_build.services.dep_plan.types import PackageNode
from oreon_build.services.distgit_order import (
    _SpecParseResult,
    _download_distgit_local_sources,
    _distgit_spec_parse_executor,
    _fetch_distgit_specs_batch,
    _spec_parse_fn,
)
from oreon_build.services.dynamic_brs import (
    spec_has_generate_buildrequires,
    spec_uses_cargo_generate_buildrequires,
)
from oreon_build.services.repo_solver import parse_depsolve_missing

logger = logging.getLogger(__name__)

_MAX_WAVES = 24


def _make_node(name: str, spec_text: str, parsed: _SpecParseResult) -> PackageNode:
    provides = set(parsed.provided_binary_names or ())
    provides.add(name)
    has_gen = bool(parsed.has_generate_buildrequires) or spec_has_generate_buildrequires(
        spec_text or ""
    )
    return PackageNode(
        name=name,
        spec_text=spec_text or "",
        provides=provides,
        exclusive_arches=set(parsed.exclusive_arches) if parsed.exclusive_arches else None,
        has_generate=has_gen,
    )


def _plan_build_defines(
    node: PackageNode,
    *,
    distro_macros: dict[str, str] | None,
    target_arches: set[str] | None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    bcond = parse_spec_bconds(
        node.spec_text or "",
        distro_macros=distro_macros,
        target_arches=target_arches,
    )
    out = default_build_defines(bcond)
    if node.rpmbuild_defines:
        out.update(node.rpmbuild_defines)
    if extra:
        out.update(extra)
    return out


def _decide_action(
    name: str,
    node: PackageNode,
    *,
    roots: set[str],
    force_rebuild: set[str],
    repo_index: Any,
    install: InstallOracle,
    distro_macros: dict[str, str] | None,
    target_arches: set[str] | None,
) -> None:
    force = name in roots or name in force_rebuild
    if not force and repo_index.satisfies_source(name):
        node.action = "skip"
        node.reason = "repo satisfies source"
        node.build_profile = "full"
        node.rpmbuild_defines = {}
        return

    if node.has_generate and not node.br_complete:
        if spec_uses_cargo_generate_buildrequires(node.spec_text) or not node.brs:
            node.action = "block"
            node.reason = node.br_error or (
                "could not evaluate cargo %generate_buildrequires (crate deps unknown)"
                if spec_uses_cargo_generate_buildrequires(node.spec_text)
                else "could not evaluate %generate_buildrequires"
            )
            return

    defines = _plan_build_defines(
        node, distro_macros=distro_macros, target_arches=target_arches
    )
    brs = filter_plan_brs(node.brs)
    if not brs:
        node.action = "build"
        node.build_profile = "full"
        node.rpmbuild_defines = defines
        node.reason = (
            "force rebuild for uninstallable published provides"
            if force and repo_index.satisfies_source(name)
            else "full build required"
        )
        return

    result = install.resolve(brs)
    if result.ok:
        node.action = "build"
        node.build_profile = "full"
        node.rpmbuild_defines = defines
        node.reason = (
            "force rebuild for uninstallable published provides"
            if force and repo_index.satisfies_source(name)
            else "full build required"
        )
        return

    if force and repo_index.satisfies_source(name):
        bcond = parse_spec_bconds(
            node.spec_text, distro_macros=distro_macros, target_arches=target_arches
        )
        profile, toggled = solve_bootstrap_defines(bcond, repo_index)
        if profile != "blocked" and toggled:
            boot = _plan_build_defines(
                node,
                distro_macros=distro_macros,
                target_arches=target_arches,
                extra=dict(toggled),
            )
            if any(str(v).strip() == "0" for k, v in toggled.items() if k.startswith("with_")):
                if (distro_macros or {}).get("oreon"):
                    boot["oreon"] = "0"
            node.action = "build"
            node.build_profile = "bootstrap"
            node.rpmbuild_defines = boot
            node.next_profile = "full"
            node.reason = "bootstrap rebuild for uninstallable published package"
            return

    misses = sorted(result.missing_provides or ())
    if not misses and result.problems:
        misses = sorted(parse_depsolve_missing(result.problems))
    from oreon_build.core.rpm_requires import is_spurious_solver_miss
    from oreon_build.services.dep_plan.br_filter import is_vacuous_rich_requirement

    misses = [
        m
        for m in misses
        if not is_vacuous_rich_requirement(m) and not is_spurious_solver_miss(m)
    ]
    if not misses:
        # problems without extractable caps: block on solver text, dont invent all BRs
        node.action = "block"
        node.reason = (
            "depsolve failed: "
            + ((result.problems or "").strip()[:400] or "unknown libsolv problem")
        )
        return
    node.action = "block"
    node.reason = "missing hard buildrequires: " + " | ".join(misses[:40])


async def _fetch_pending_specs(
    pending: set[str],
    *,
    specs_by_pkg: dict[str, str],
    parsed_by_source: dict[str, _SpecParseResult],
    nodes: dict[str, PackageNode],
    releasename: str,
    owner: str,
    repo: str,
    distgit_branch: str,
    headers: dict[str, str],
    arches: set[str] | None,
) -> list[str]:
    need = sorted(p for p in pending if p not in parsed_by_source)
    if not need:
        return []
    errors: list[str] = []
    parse_fn = _spec_parse_fn(releasename, target_arches=arches)
    pool = _distgit_spec_parse_executor()
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(
        timeout=timeout, headers=headers, follow_redirects=True
    ) as client:
        batch = await _fetch_distgit_specs_batch(
            client,
            owner=owner,
            repo=repo,
            distgit_branch=distgit_branch,
            releasename=releasename,
            packages=need,
        )
    for pkg in need:
        if pkg not in batch:
            errors.append(f"{pkg}: not in distgit at {releasename}/")
            continue
        specs_by_pkg[pkg] = batch[pkg]
    futs = {
        pool.submit(parse_fn, batch[pkg], f"{pkg}.spec"): pkg
        for pkg in batch
    }
    for fut in as_completed(futs):
        pkg = futs[fut]
        try:
            parsed = fut.result()
        except Exception as exc:
            errors.append(f"{pkg}: spec parse failed: {exc}")
            continue
        parsed_by_source[pkg] = parsed
        nodes[pkg] = _make_node(pkg, specs_by_pkg.get(pkg, ""), parsed)
    return errors


async def expand_fixed_point(
    *,
    roots: list[str],
    specs_by_pkg: dict[str, str],
    parsed_by_source: dict[str, _SpecParseResult],
    repo_index: Any,
    install: InstallOracle,
    provider_map: dict[str, set[str]],
    package_names: set[str],
    releasename: str,
    owner: str,
    repo: str,
    distgit_branch: str,
    headers: dict[str, str],
    architectures: list[str],
    distro_macros: dict[str, str] | None,
) -> tuple[dict[str, PackageNode], dict[str, set[str]], list[str], list[str], list[str]]:
    arches = {a.lower() for a in (architectures or []) if a} or None
    macros = distro_macros or default_distro_macros(releasename)
    root_set = {(r or "").strip() for r in roots if (r or "").strip()}
    nodes: dict[str, PackageNode] = {}
    provider_edges: dict[str, set[str]] = {}
    path_errors: list[str] = []
    import_caps: list[str] = []
    errors: list[str] = []
    pending = set(root_set)
    force_rebuild: set[str] = set()

    for name, parsed in list(parsed_by_source.items()):
        nodes[name] = _make_node(name, specs_by_pkg.get(name, ""), parsed)

    with tempfile.TemporaryDirectory(prefix="oreon-depplan-src-") as tmp:
        tmp_path = Path(tmp)
        for wave in range(_MAX_WAVES):
            ferr = await _fetch_pending_specs(
                pending,
                specs_by_pkg=specs_by_pkg,
                parsed_by_source=parsed_by_source,
                nodes=nodes,
                releasename=releasename,
                owner=owner,
                repo=repo,
                distgit_branch=distgit_branch,
                headers=headers,
                arches=arches,
            )
            errors.extend(ferr)

            eval_items: list[tuple[str, str, list[Path] | None]] = []
            for name, node in nodes.items():
                if node.br_evaluated and node.br_complete and node.br_pass_kind != "pass1_static":
                    continue
                if node.br_evaluated and node.br_pass_kind == "pass1_static":
                    continue
                text = node.spec_text or specs_by_pkg.get(name, "")
                if not text:
                    continue
                dest = tmp_path / name
                paths = await asyncio.to_thread(
                    _download_distgit_local_sources,
                    pkg=name,
                    spec_text=text,
                    releasename=releasename,
                    owner=owner,
                    repo=repo,
                    distgit_branch=distgit_branch,
                    headers=headers,
                    distro_macros=macros,
                    dest_dir=dest,
                )
                eval_items.append((name, text, paths or None))

            if eval_items:
                results = await asyncio.to_thread(
                    evaluate_many_brs,
                    eval_items,
                    distro_macros=macros,
                    target_arches=arches,
                    max_workers=6,
                )
                for name, br in results.items():
                    node = nodes[name]
                    node.brs = list(br.buildrequires)
                    if not node.brs and name in parsed_by_source:
                        node.brs = list(parsed_by_source[name].build_requires or [])
                    node.br_error = br.error or ""
                    node.br_evaluated = True
                    node.br_pass_kind = br.pass_kind or ("final" if br.complete else "pass1_static")
                    if br.pass_kind == "pass1_static":
                        # schedule generate tools via static BRs; usable for solv
                        node.br_complete = True
                    else:
                        node.br_complete = bool(br.complete)
                    if name in parsed_by_source:
                        parsed_by_source[name].rpm_build_requires = list(node.brs)
                        parsed_by_source[name].dyn_br_complete = bool(node.br_complete)
                        if node.has_generate:
                            parsed_by_source[name].has_generate_buildrequires = True

            # re-run generate for pass1 once static tools resolve under overlay
            reeval: list[tuple[str, str, list[Path] | None]] = []
            install.set_overlay(
                overlay_from_nodes(
                    {
                        n: nodes[n]
                        for n in nodes
                        if nodes[n].action in {"build", "pending", "skip"} or n in root_set
                    },
                    provider_map,
                )
            )
            for name, node in nodes.items():
                if node.br_pass_kind != "pass1_static":
                    continue
                static = filter_plan_brs(node.brs)
                if static and install.can_install(static):
                    text = node.spec_text or specs_by_pkg.get(name, "")
                    if text:
                        reeval.append((name, text, None))
            if reeval:
                fixed: list[tuple[str, str, list[Path] | None]] = []
                for name, text, _ in reeval:
                    dest = tmp_path / name
                    paths = await asyncio.to_thread(
                        _download_distgit_local_sources,
                        pkg=name,
                        spec_text=text,
                        releasename=releasename,
                        owner=owner,
                        repo=repo,
                        distgit_branch=distgit_branch,
                        headers=headers,
                        distro_macros=macros,
                        dest_dir=dest,
                    )
                    fixed.append((name, text, paths or None))
                results = await asyncio.to_thread(
                    evaluate_many_brs,
                    fixed,
                    distro_macros=macros,
                    target_arches=arches,
                    max_workers=4,
                    force_refresh=True,
                )
                for name, br in results.items():
                    node = nodes[name]
                    node.brs = list(br.buildrequires)
                    node.br_complete = bool(br.complete)
                    node.br_error = br.error or ""
                    node.br_pass_kind = br.pass_kind or ("final" if br.complete else "pass1_static")
                    node.br_evaluated = True
                    if name in parsed_by_source:
                        parsed_by_source[name].rpm_build_requires = list(br.buildrequires)
                        parsed_by_source[name].dyn_br_complete = bool(br.complete)

            install.set_overlay(overlay_from_nodes(nodes, provider_map))
            for name, node in list(nodes.items()):
                _decide_action(
                    name,
                    node,
                    roots=root_set,
                    force_rebuild=force_rebuild,
                    repo_index=repo_index,
                    install=install,
                    distro_macros=macros,
                    target_arches=arches,
                )

            new_pending: set[str] = set()
            known_pkgs = package_names | set(nodes)
            wave_path: list[str] = []
            wave_imps: list[str] = []
            forced_this_wave = False
            for name, node in list(nodes.items()):
                if node.action == "skip":
                    continue
                brs = filter_plan_brs(node.brs)
                solve = install.resolve(brs) if brs else None
                if node.action == "build" and (solve is None or solve.ok):
                    for br in brs:
                        for src in providers_for_requirement(
                            br,
                            provider_map=provider_map,
                            repo_index=repo_index,
                            package_names=known_pkgs,
                        ):
                            if src == name:
                                continue
                            if src in nodes and nodes[src].action == "build":
                                provider_edges.setdefault(name, set()).add(src)
                            elif (
                                is_distgit_source_name(src)
                                and not repo_index.satisfies_source(src)
                            ):
                                provider_edges.setdefault(name, set()).add(src)
                                if src not in nodes:
                                    new_pending.add(src)
                            elif (
                                is_distgit_source_name(src)
                                and repo_index.satisfies_source(src)
                                and src in nodes
                                and nodes[src].action == "skip"
                            ):
                                # published provider but consumer still needs its Provides
                                # (handled below when consumer is blocked)
                                pass
                    continue

                pe, imps, fetch = classify_misses(
                    root=name,
                    nodes=nodes,
                    solve=solve,
                    brs=brs,
                    provider_map=provider_map,
                    repo_index=repo_index,
                    package_names=known_pkgs,
                    install=install,
                )
                if name in root_set or node.action == "block":
                    wave_path.extend(pe)
                    wave_imps.extend(imps)
                for src in fetch:
                    provider_edges.setdefault(name, set()).add(src)
                    if src not in nodes:
                        new_pending.add(src)
                        continue
                    if nodes[src].action == "skip" or (
                        repo_index.satisfies_source(src) and src not in force_rebuild
                    ):
                        force_rebuild.add(src)
                        nodes[src].action = "pending"
                        forced_this_wave = True

            path_errors = wave_path
            import_caps = wave_imps

            if not new_pending and not forced_this_wave:
                logger.info(
                    "dep_plan expand stable wave=%s nodes=%s force=%s",
                    wave,
                    len(nodes),
                    len(force_rebuild),
                )
                break
            pending = new_pending
            logger.info(
                "dep_plan expand wave=%s nodes=%s pending=%s force=%s",
                wave,
                len(nodes),
                len(pending),
                len(force_rebuild),
            )
        else:
            errors.append(f"dependency expand hit max waves ({_MAX_WAVES})")

    # cascade prune non-root builds that still cannot resolve via selected set
    changed = True
    while changed:
        changed = False
        build_set = {n for n, node in nodes.items() if node.action == "build"}
        install.set_overlay(
            overlay_from_nodes({n: nodes[n] for n in build_set}, provider_map)
        )
        known_pkgs = package_names | set(nodes)
        for name in sorted(build_set):
            if name in root_set:
                continue
            node = nodes[name]
            brs = filter_plan_brs(node.brs)
            if not brs:
                continue
            result = install.resolve(brs)
            if result.ok:
                continue
            misses = set(result.missing_provides or ())
            if not misses and result.problems:
                misses = parse_depsolve_missing(result.problems)
            if not misses:
                node.action = "block"
                node.reason = (
                    "depsolve failed: "
                    + ((result.problems or "").strip()[:400] or "unknown libsolv problem")
                )
                path_errors.append(f"{name}: {node.reason}")
                changed = True
                continue
            covered = True
            for miss in misses:
                from oreon_build.core.rpm_requires import is_spurious_solver_miss

                if is_spurious_solver_miss(miss):
                    continue
                if install.has_provider(miss):
                    continue
                provs = providers_for_requirement(
                    miss,
                    provider_map=provider_map,
                    repo_index=repo_index,
                    package_names=known_pkgs,
                )
                if provs & build_set:
                    continue
                if any(repo_index.satisfies_source(s) for s in provs):
                    continue
                covered = False
                break
            if not covered:
                node.action = "block"
                node.reason = "missing hard buildrequires: " + " | ".join(
                    sorted(misses)[:40]
                )
                path_errors.append(f"{name}: {node.reason}")
                changed = True

    # final edges among builds
    build_set = {n for n, node in nodes.items() if node.action == "build"}
    known_pkgs = package_names | set(nodes)
    install.set_overlay(overlay_from_nodes({n: nodes[n] for n in build_set}, provider_map))
    for name in build_set:
        for br in filter_plan_brs(nodes[name].brs):
            for src in providers_for_requirement(
                br,
                provider_map=provider_map,
                repo_index=repo_index,
                package_names=known_pkgs,
            ):
                if src in build_set and src != name:
                    provider_edges.setdefault(name, set()).add(src)

    return (
        nodes,
        provider_edges,
        dedupe_keep_order(path_errors),
        dedupe_keep_order(import_caps),
        dedupe_keep_order(errors),
    )
