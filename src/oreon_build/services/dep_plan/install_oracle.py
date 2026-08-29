"""Installability oracle: libsolv only (repos + plan overlay)."""

from __future__ import annotations

import logging
from typing import Any, Iterable

from oreon_build.services.repo_solver import (
    MultiArchRepoSolver,
    OverlayPackage,
    SolveResult,
)

logger = logging.getLogger(__name__)

_SOURCE_CAPS_CACHE: dict[tuple[int, int], dict[str, set[str]]] = {}
_SOURCE_CAPS_CACHE_MAX = 4


def _source_to_caps(provider_map: dict[str, Any]) -> dict[str, set[str]]:
    """Invert cap->sources into source->caps once per provider_map version.

    overlay_from_nodes used to scan the whole (200k+ entry) provider_map on
    every call to find what a handful of in-flight nodes provide. Caching
    the inverted index turns that per-call O(len(provider_map)) scan into an
    O(1) lookup on repeat calls with the same map. Keyed by (identity, len)
    rather than identity alone so an in-place mutation (new key added)
    invalidates the cache instead of silently returning stale results.
    """
    key = (id(provider_map), len(provider_map))
    hit = _SOURCE_CAPS_CACHE.get(key)
    if hit is not None:
        return hit
    inverted: dict[str, set[str]] = {}
    for cap, srcs in (provider_map or {}).items():
        if cap == "__lower__" or not isinstance(srcs, (set, frozenset, list, tuple)):
            continue
        for src in srcs:
            if src:
                inverted.setdefault(src, set()).add(cap)
    if len(_SOURCE_CAPS_CACHE) >= _SOURCE_CAPS_CACHE_MAX:
        _SOURCE_CAPS_CACHE.clear()
    _SOURCE_CAPS_CACHE[key] = inverted
    return inverted


class InstallOracle:
    def __init__(self, solver: MultiArchRepoSolver) -> None:
        if solver is None:
            raise RuntimeError("InstallOracle requires a libsolv MultiArchRepoSolver")
        self._solver = solver
        self._overlay_fp = ""
        self._cache: dict[tuple[str, ...], SolveResult] = {}

    @property
    def available(self) -> bool:
        return True

    def set_overlay(self, packages: dict[str, OverlayPackage]) -> None:
        self._solver.set_overlay(packages)
        fp = "|".join(
            f"{k}={getattr(v, 'epoch', 0)}:{getattr(v, 'version', '0')}-"
            f"{getattr(v, 'release', '0')}+{','.join(sorted(getattr(v, 'provides', ()) or ())) }"
            f"|req={','.join(sorted(getattr(v, 'requires', ()) or ())) }"
            for k, v in sorted(packages.items())
        )
        if fp != self._overlay_fp:
            self._overlay_fp = fp
            self._cache.clear()

    def clear_overlay(self) -> None:
        self.set_overlay({})

    def resolve(self, requirements: Iterable[str]) -> SolveResult:
        from oreon_build.core.rpm_requires import requirement_usable_for_solver

        reqs = tuple(
            sorted(
                {
                    usable
                    for r in requirements
                    if (usable := requirement_usable_for_solver(r or ""))
                }
            )
        )
        if not reqs:
            return SolveResult(ok=True)
        key = (self._overlay_fp, *reqs)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        result = self._solver.resolve(reqs)
        self._cache[key] = result
        return result

    def can_install(self, requirements: Iterable[str]) -> bool:
        return self.resolve(requirements).ok

    def has_provider(self, requirement: str) -> bool:
        return bool(self._solver.has_provider(requirement))

    def sources_providing(self, requirement: str) -> set[str]:
        return set(self._solver.sources_providing(requirement))


def overlay_from_nodes(
    nodes: dict[str, Any],
    provider_map: dict[str, set[str]],
    *,
    actions: set[str] | None = None,
) -> dict[str, OverlayPackage]:
    """Overlay solvables for packages we still intend to produce.

    Only build/pending nodes. Blocked/skipped must not fake Provides or
    consumers look installable while their deps can never ship.
    """
    import re

    from oreon_build.services.distgit_order import _rust_crate_name_from_package

    allowed = actions if actions is not None else {"build", "pending"}
    want = {
        n
        for n, node in nodes.items()
        if (getattr(node, "action", "pending") or "pending") in allowed
    }
    source_caps = _source_to_caps(provider_map)
    by_src: dict[str, set[str]] = {n: source_caps[n] for n in want if n in source_caps}

    out: dict[str, OverlayPackage] = {}
    for name, node in nodes.items():
        act = getattr(node, "action", "pending") or "pending"
        if act not in allowed:
            continue
        caps = set(by_src.get(name, set()))
        caps.add(name)
        requires = {
            str(req).strip()
            for req in (getattr(node, "runtime_requires", None) or ())
            if str(req).strip() and "%" not in str(req)
        }
        provides = getattr(node, "provides", None) or set()
        caps |= {c for c in provides if c}
        for b in getattr(node, "binaries", None) or ():
            if b:
                caps.add(b)
        crate = _rust_crate_name_from_package(
            name, version=(getattr(node, "version", None) or "").strip() or None
        )
        if crate:
            caps.add(f"crate({crate})")
        for cap in list(caps):
            m = re.match(r"^rust-(?P<body>.+)\+(?P<feat>[^+]+)-devel$", cap)
            if not m:
                continue
            body = m.group("body")
            feat = m.group("feat")
            body_crate = _rust_crate_name_from_package(f"rust-{body}") or crate
            if not body_crate:
                body_crate = _rust_crate_name_from_package(f"rust-{body}") or body
            caps.add(f"crate({body_crate})")
            caps.add(f"crate({body_crate}/{feat})")
        clean = {c for c in caps if _cap_ok(c)}
        if not clean:
            clean = {name}
        ver = (getattr(node, "version", None) or "").strip() or "0"
        if "%" in ver:
            ver = "0"
        rel = (getattr(node, "release", None) or "").strip() or "0"
        if "%" in rel:
            rel = "0"
        epoch = int(getattr(node, "epoch", 0) or 0)
        out[name] = OverlayPackage(
            source_name=name,
            provides=frozenset(clean),
            requires=frozenset(requires),
            epoch=epoch,
            version=ver,
            release=rel,
        )
    return out


def _cap_ok(cap: str) -> bool:
    c = (cap or "").strip()
    if not c or len(c) > 220 or "%" in c:
        return False
    if any(ch in c for ch in ("'", '"', "`", "$", ";", "|", "{", "}", "\\", "\n", "\t")):
        return False
    if c.startswith(("(", "=", "<", ">", "!")):
        return False
    return bool(c)
