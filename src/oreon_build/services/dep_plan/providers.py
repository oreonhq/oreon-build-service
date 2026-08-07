"""Provider map only: capability → distgit source names. Never installability."""

from __future__ import annotations

import re
from typing import Any

from oreon_build.core.rpm_requires import (
    evr_satisfies,
    is_distgit_source_name,
    merge_provider_maps,
    requirement_check_capabilities,
    sources_providing_requirement,
    split_requirement_evr,
)
from oreon_build.services.dep_plan.br_filter import filter_plan_brs
from oreon_build.services.repo_capability_index import is_rich_dependency, is_rpmlib_requirement

__all__ = [
    "actionable_caps",
    "filter_plan_brs",
    "merge_maps",
    "providers_for_requirement",
]

_RE_CRATE_VER_LEAF = re.compile(
    r"crate\((?P<inner>[^)]+)\)\s*(?P<op>>=|<=|>|<|=|==)\s*(?P<ver>[^\s,)]+)"
)


def merge_maps(*maps: dict[str, set[str]] | None) -> dict[str, set[str]]:
    return merge_provider_maps(*[m for m in maps if m])


def _map_sources(provider_map: dict, cap: str) -> set[str]:
    from oreon_build.services.virtual_resolve import _pmap_sources

    return _pmap_sources(provider_map, cap, (cap or "").lower())


def _rust_compat_series_from_package(pkg: str) -> str | None:
    """rust-rustix0.37 → 0.37, rust-nom7 → 7, rust-rustix → None."""
    from oreon_build.services.distgit_order import _rust_crate_name_from_package

    n = (pkg or "").strip()
    crate = _rust_crate_name_from_package(n)
    if not crate or not n.startswith("rust-"):
        return None
    rest = n[5:]
    if not rest.startswith(crate):
        return None
    suffix = rest[len(crate) :]
    if re.fullmatch(r"0\.\d+(?:\.\d+)*", suffix) or re.fullmatch(r"\d+", suffix):
        return suffix
    return None


def _crate_version_constraints(requirement: str) -> list[tuple[str, str, str]]:
    """(crate_name, op, ver) leaves from rich/plain crate Requires."""
    raw = (requirement or "").strip()
    if not raw or "crate(" not in raw:
        return []
    out: list[tuple[str, str, str]] = []
    for m in _RE_CRATE_VER_LEAF.finditer(raw):
        inner = (m.group("inner") or "").strip()
        op = (m.group("op") or "").strip()
        ver = (m.group("ver") or "").strip()
        if not inner or not op or not ver or "%" in ver:
            continue
        crate = inner.split("/", 1)[0].strip()
        if not crate:
            continue
        if op == "==":
            op = "="
        out.append((crate, op, ver))
    return out


def _series_as_provide_ver(series: str) -> str:
    """Compat dir 0.37 means 0.37.x; pad so rpmvercmp matches cargo bounds."""
    s = (series or "").strip()
    if not s:
        return "0"
    parts = s.split(".")
    if len(parts) == 2 and parts[0] == "0" and parts[1].isdigit():
        return f"{s}.0"
    if len(parts) == 1 and parts[0].isdigit():
        return s
    return s


def _series_next(series: str) -> str | None:
    """Next cargo-compat series boundary: 0.1 → 0.2, 7 → 8."""
    s = (series or "").strip()
    parts = s.split(".")
    if len(parts) == 2 and parts[0] == "0" and parts[1].isdigit():
        return f"0.{int(parts[1]) + 1}"
    if len(parts) == 1 and parts[0].isdigit():
        return str(int(parts[0]) + 1)
    return None


def _source_may_satisfy_crate_versions(src: str, requirement: str) -> bool:
    """False only when a compat-suffix package cannot meet crate version bounds.

    Compat dirs name a series range [series, next), not a single version.
    rust-structmeta0.1 can satisfy >= 0.1.4 with < 0.2 even though the
    series token pads to 0.1.0. Unsuffixed rust-foo is always kept.
    """
    constraints = _crate_version_constraints(requirement)
    if not constraints:
        return True
    from oreon_build.services.distgit_order import _rust_crate_name_from_package

    src_crate = _rust_crate_name_from_package(src)
    series = _rust_compat_series_from_package(src)
    if series is None:
        return True
    nxt = _series_next(series)
    if nxt is None:
        return True
    floor = split_requirement_evr(_series_as_provide_ver(series))
    ceiling = split_requirement_evr(_series_as_provide_ver(nxt))
    for crate, op, ver in constraints:
        if src_crate and src_crate != crate:
            continue
        need = split_requirement_evr(ver)
        if op == ">=":
            if not evr_satisfies("<", need, ceiling):
                return False
        elif op == ">":
            if not evr_satisfies("<", need, ceiling):
                return False
        elif op == "<":
            if not evr_satisfies("<", floor, need):
                return False
        elif op == "<=":
            if not (
                evr_satisfies("<", floor, need) or evr_satisfies("=", floor, need)
            ):
                return False
        elif op == "=":
            if evr_satisfies(">", floor, need):
                return False
            if not evr_satisfies("<", need, ceiling):
                return False
    return True


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
    for cap in requirement_check_capabilities(raw):
        if cap in known and is_distgit_source_name(cap):
            out.add(cap)
        for src in _map_sources(provider_map, cap):
            if is_distgit_source_name(src):
                out.add(src)

    from oreon_build.services.perl_resolve import distgit_sources_for_perl_module
    from oreon_build.core.rpm_requires import rpm_requirement_primary

    perl_mods: list[str] = []
    primary = rpm_requirement_primary(raw) or raw
    if primary and primary.startswith("perl(") and primary.endswith(")"):
        perl_mods.append(primary[5:-1])
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
    if "crate(" in raw and _crate_version_constraints(raw):
        out = {s for s in out if _source_may_satisfy_crate_versions(s, raw)}
    return out


def actionable_caps(requirement: str) -> list[str]:
    raw = (requirement or "").strip()
    if not raw or is_rpmlib_requirement(raw):
        return []
    if is_rich_dependency(raw):
        return [c for c in requirement_check_capabilities(raw) if c]
    return requirement_check_capabilities(raw) or [raw]
