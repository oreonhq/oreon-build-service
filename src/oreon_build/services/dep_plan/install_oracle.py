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
            f"{k}={','.join(sorted(getattr(v, 'provides', ()) or ()))}"
            for k, v in sorted(packages.items())
        )
        if fp != self._overlay_fp:
            self._overlay_fp = fp
            self._cache.clear()

    def clear_overlay(self) -> None:
        self.set_overlay({})

    def resolve(self, requirements: Iterable[str]) -> SolveResult:
        reqs = tuple(
            sorted(
                {
                    (r or "").strip()
                    for r in requirements
                    if (r or "").strip() and "%" not in (r or "")
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
) -> dict[str, OverlayPackage]:
    """Build overlay solvables for packages we plan to build (Provides only)."""
    import re

    from oreon_build.services.distgit_order import _rust_crate_name_from_package

    by_src: dict[str, set[str]] = {}
    want = set(nodes)
    for cap, srcs in (provider_map or {}).items():
        if cap == "__lower__" or not isinstance(srcs, (set, frozenset, list, tuple)):
            continue
        hit = want.intersection(srcs) if not isinstance(srcs, set) else (want & srcs)
        if not hit:
            continue
        for src in hit:
            by_src.setdefault(src, set()).add(cap)

    out: dict[str, OverlayPackage] = {}
    for name, node in nodes.items():
        caps = set(by_src.get(name, set()))
        caps.add(name)
        provides = getattr(node, "provides", None) or set()
        caps |= {c for c in provides if c}
        for b in getattr(node, "binaries", None) or ():
            if b:
                caps.add(b)
        crate = _rust_crate_name_from_package(name)
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
        out[name] = OverlayPackage(source_name=name, provides=frozenset(clean))
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
