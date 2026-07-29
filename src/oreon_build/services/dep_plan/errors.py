"""Accurate plan errors: import vs in-distgit blocked path."""

from __future__ import annotations

from typing import Any

from oreon_build.core.rpm_requires import is_distgit_source_name
from oreon_build.services.dep_plan.providers import providers_for_requirement
from oreon_build.services.dep_plan.types import PackageNode
from oreon_build.services.repo_solver import SolveResult


def classify_misses(
    *,
    root: str,
    nodes: dict[str, PackageNode],
    solve: SolveResult | None,
    brs: list[str],
    provider_map: dict[str, set[str]],
    repo_index: Any | None,
    package_names: set[str],
    install: Any | None = None,
) -> tuple[list[str], list[str], set[str]]:
    """Return (path_errors, import_caps, fetch_sources).

    import only when solv pool has zero providers AND distgit has no source.
    """
    path_errors: list[str] = []
    imports: list[str] = []
    fetch: set[str] = set()

    node = nodes.get(root)
    if node is not None and not node.br_complete and node.action == "block":
        path_errors.append(f"{root}: {node.reason or 'could not evaluate %generate_buildrequires'}")
        return path_errors, imports, fetch

    if solve is not None and solve.ok:
        return path_errors, imports, fetch

    misses: list[str] = []
    if solve is not None and not solve.ok:
        misses = sorted(solve.missing_provides or ())
        if not misses and solve.problems:
            from oreon_build.services.repo_solver import parse_depsolve_missing

            misses = sorted(parse_depsolve_missing(solve.problems))
        # never invent miss=all BRs

    for miss in misses:
        from oreon_build.core.rpm_requires import is_spurious_solver_miss
        from oreon_build.services.dep_plan.br_filter import is_vacuous_rich_requirement

        if is_vacuous_rich_requirement(miss) or is_spurious_solver_miss(miss):
            continue

        solv_srcs: set[str] = set()
        has_solv = False
        if install is not None:
            has_fn = getattr(install, "has_provider", None)
            src_fn = getattr(install, "sources_providing", None)
            if callable(has_fn):
                try:
                    has_solv = bool(has_fn(miss))
                except Exception:
                    has_solv = False
            if callable(src_fn):
                try:
                    solv_srcs = {
                        s
                        for s in (src_fn(miss) or ())
                        if is_distgit_source_name(str(s))
                    }
                except Exception:
                    solv_srcs = set()

        if has_solv or solv_srcs:
            # repo already Provides this. never toast import.
            for src in sorted(solv_srcs):
                if src in nodes:
                    child = nodes[src]
                    if child.action == "block":
                        path_errors.append(f"{src}: {child.reason or 'blocked'}")
                    else:
                        fetch.add(src)
                else:
                    fetch.add(src)
            if not solv_srcs and has_solv:
                # providers exist but sourcerpm map empty → block with accurate miss
                path_errors.append(
                    f"{root}: uninstallable provide present in repos: {miss}"
                )
            continue

        provs = providers_for_requirement(
            miss,
            provider_map=provider_map,
            repo_index=repo_index,
            package_names=package_names,
        )
        distgit_hits = {s for s in provs if is_distgit_source_name(s)}
        if not distgit_hits:
            if is_distgit_source_name(miss) and (
                miss in package_names or miss in nodes
            ):
                fetch.add(miss)
                child = nodes.get(miss)
                if child is not None and child.action == "block":
                    path_errors.append(
                        f"{miss}: {child.reason or 'blocked'}"
                    )
                continue
            from oreon_build.core.rpm_requires import is_spurious_solver_miss
            from oreon_build.services.virtual_resolve import import_suggestion_for_cap

            suggestion = import_suggestion_for_cap(miss) or miss
            if is_spurious_solver_miss(suggestion):
                path_errors.append(f"{root}: unresolved capability: {miss}")
                continue
            # suggestion may be a binary subpackage name (%package -n) of an existing source
            if suggestion != miss:
                alt = providers_for_requirement(
                    suggestion,
                    provider_map=provider_map,
                    repo_index=repo_index,
                    package_names=package_names,
                )
                alt_hits = {s for s in alt if is_distgit_source_name(s)}
                if alt_hits:
                    for src in sorted(alt_hits):
                        if src in nodes and nodes[src].action == "block":
                            path_errors.append(
                                f"{src}: {nodes[src].reason or 'blocked'}"
                            )
                        else:
                            fetch.add(src)
                    continue
                if suggestion in package_names or suggestion in nodes:
                    fetch.add(suggestion)
                    continue
                # rewritten virtual/devel name with no providers anywhere:
                # retired or never packaged (hal-devel → hal). not an import hole.
                path_errors.append(f"{root}: no providers for {miss}")
                continue
            imports.append(suggestion)
            continue
        for src in sorted(distgit_hits):
            if src in nodes:
                child = nodes[src]
                if child.action == "block":
                    path_errors.append(f"{src}: {child.reason or 'blocked'}")
                else:
                    fetch.add(src)
            else:
                fetch.add(src)

    if node is not None and node.action == "block" and not path_errors:
        path_errors.append(f"{root}: {node.reason or 'blocked'}")

    return path_errors, imports, fetch


def root_coverage_errors(
    roots: list[str],
    nodes: dict[str, PackageNode],
    path_errors: list[str],
) -> list[str]:
    covered = {
        n
        for n, node in nodes.items()
        if node.action in {"build", "skip"}
    }
    uncovered = [r for r in roots if r not in covered]
    if not uncovered:
        return []
    if path_errors:
        return list(path_errors)
    return [f"requested package(s) not scheduled: {', '.join(uncovered)}"]


def dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        s = (x or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out
