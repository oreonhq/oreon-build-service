"""Provider map only: capability → distgit source names. Never installability."""

from __future__ import annotations

from typing import Any

from oreon_build.core.rpm_requires import (
    is_distgit_source_name,
    merge_provider_maps,
    requirement_check_capabilities,
    sources_providing_requirement,
)
from oreon_build.services.dep_plan.br_filter import filter_plan_brs
from oreon_build.services.repo_capability_index import is_rich_dependency, is_rpmlib_requirement

__all__ = [
    "actionable_caps",
    "filter_plan_brs",
    "merge_maps",
    "providers_for_requirement",
]


def merge_maps(*maps: dict[str, set[str]] | None) -> dict[str, set[str]]:
    return merge_provider_maps(*[m for m in maps if m])


def _map_sources(provider_map: dict, cap: str) -> set[str]:
    from oreon_build.services.virtual_resolve import _pmap_sources

    return _pmap_sources(provider_map, cap, (cap or "").lower())


def providers_for_requirement(
    requirement: str,
    *,
    provider_map: dict[str, set[str]],
    repo_index: Any | None = None,
    package_names: set[str] | None = None,
) -> set[str]:
    """Sources that Provide this cap (or rich-dep leaves). Map lookup only."""
    raw = (requirement or "").strip()
    if not raw or is_rpmlib_requirement(raw):
        return set()
    out: set[str] = set()
    known = package_names or set()
    if repo_index is not None:
        out |= {
            s
            for s in sources_providing_requirement(
                raw, repo_index, provider_maps=[provider_map], exact=True
            )
            if is_distgit_source_name(s)
        }
    else:
        for cap in requirement_check_capabilities(raw):
            for src in _map_sources(provider_map, cap):
                if is_distgit_source_name(src):
                    out.add(src)
            lower = provider_map.get("__lower__")
            if isinstance(lower, dict):
                for src in _map_sources(lower, cap.lower()):
                    if is_distgit_source_name(src):
                        out.add(src)
    # aliases like rust-foo that are themselves distgit sources
    for cap in requirement_check_capabilities(raw):
        if cap in known and is_distgit_source_name(cap):
            out.add(cap)
        for src in _map_sources(provider_map, cap):
            if is_distgit_source_name(src):
                out.add(src)
    # perl(Module) → parent packages / metacpan dist (discovery only, not installability)
    import re

    from oreon_build.services.perl_resolve import distgit_sources_for_perl_module

    perl_mods: list[str] = []
    if raw.startswith("perl(") and raw.endswith(")"):
        perl_mods.append(raw[5:-1])
    elif raw.startswith("(") and "perl(" in raw:
        for cap in requirement_check_capabilities(raw):
            m = re.match(r"^perl\(([^)]+)\)$", cap)
            if m:
                perl_mods.append(m.group(1))
    else:
        m = re.match(r"^perl\(([^)]+)\)$", raw)
        if m:
            perl_mods.append(m.group(1))
    for mod in perl_mods:
        out |= {
            s
            for s in distgit_sources_for_perl_module(mod, known)
            if is_distgit_source_name(s)
        }
    from oreon_build.services.virtual_resolve import distgit_sources_for_virtual_cap

    out |= {
        s
        for s in distgit_sources_for_virtual_cap(
            raw, package_names=known, provider_map=provider_map
        )
        if is_distgit_source_name(s)
    }
    if not out and is_distgit_source_name(raw):
        if raw in known:
            out.add(raw)
        else:
            mapped = _map_sources(provider_map, raw)
            if mapped:
                out |= {s for s in mapped if is_distgit_source_name(s)}
            elif repo_index is not None and (
                raw in getattr(repo_index, "source_names", ())
                or getattr(repo_index, "satisfies_source", lambda _n: False)(raw)
            ):
                out.add(raw)
    return out


def actionable_caps(requirement: str) -> list[str]:
    raw = (requirement or "").strip()
    if not raw or is_rpmlib_requirement(raw):
        return []
    if is_rich_dependency(raw):
        return [c for c in requirement_check_capabilities(raw) if c]
    return requirement_check_capabilities(raw) or [raw]
