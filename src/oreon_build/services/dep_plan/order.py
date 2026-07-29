"""Build order via networkx topological_generations."""

from __future__ import annotations

from typing import Iterable

from oreon_build.services.dep_plan.types import PackageNode, PlannedPackage


def topo_layers(
    names: set[str],
    deps: dict[str, set[str]],
) -> tuple[list[list[str]], bool]:
    """deps[consumer] = providers that must build first."""
    import networkx as nx

    if not names:
        return [], False

    rdeps: dict[str, set[str]] = {n: set() for n in names}
    G = nx.DiGraph()
    G.add_nodes_from(names)
    for cons, provs in deps.items():
        if cons not in names:
            continue
        for p in provs:
            if p in names and p != cons:
                G.add_edge(p, cons)
                rdeps.setdefault(p, set()).add(cons)

    cycle = False
    layers: list[list[str]] = []
    remaining = set(names)
    while remaining:
        sub = G.subgraph(remaining)
        try:
            for gen in nx.topological_generations(sub):
                layers.append(sorted(gen))
            return layers, cycle
        except nx.NetworkXUnfeasible:
            cycle = True
            in_deg = {n: int(sub.in_degree(n)) for n in remaining}
            pick = min(
                remaining,
                key=lambda n: (
                    in_deg.get(n, 0),
                    -sum(1 for d in rdeps.get(n, set()) if d in remaining),
                    n,
                ),
            )
            layers.append([pick])
            remaining.discard(pick)
    return layers, cycle


def build_order_edges(
    nodes: dict[str, PackageNode],
    *,
    provider_edges: dict[str, set[str]],
) -> dict[str, set[str]]:
    """deps[consumer] = providers in the selected build set."""
    selected = {n for n, node in nodes.items() if node.action == "build"}
    deps: dict[str, set[str]] = {n: set() for n in selected}
    for cons, provs in provider_edges.items():
        if cons not in selected:
            continue
        for p in provs:
            if p in selected and p != cons:
                deps[cons].add(p)
    return deps


def layers_to_planned(
    layer_names: list[list[str]],
    nodes: dict[str, PackageNode],
) -> list[list[PlannedPackage]]:
    out: list[list[PlannedPackage]] = []
    for layer in layer_names:
        pkgs = [
            PlannedPackage(
                name=n,
                action=nodes[n].action,
                build_profile=nodes[n].build_profile,
                rpmbuild_defines=dict(nodes[n].rpmbuild_defines),
                next_profile=nodes[n].next_profile,
                reason=nodes[n].reason,
            )
            for n in layer
            if n in nodes and nodes[n].action == "build"
        ]
        if pkgs:
            out.append(pkgs)
    return out


def chain_spec_from_layers(layers: list[list[PlannedPackage]]) -> str:
    groups: list[str] = []
    for layer in layers:
        names = [p.name for p in layer if p.action == "build"]
        if names:
            groups.append(" ".join(names))
    return " : ".join(groups)


def package_plan_dict(node: PackageNode) -> dict:
    out = {
        "build_profile": node.build_profile,
        "rpmbuild_defines": dict(node.rpmbuild_defines),
    }
    if node.next_profile:
        out["next_profile"] = node.next_profile
    if node.exclusive_arches:
        out["exclusive_arches"] = sorted(a for a in node.exclusive_arches if a)
    return out
