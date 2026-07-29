"""Facade: automatic chain planning via dep_plan three-oracle engine."""

from __future__ import annotations

import re
from typing import Any

from oreon_build.services.dep_plan.planner import (
    fetch_and_plan_automatic_chain,
    plan_automatic_chain,
)
from oreon_build.services.dep_plan.types import BuildPlanResult, PlannedPackage

# re-exports for callers / tests
from oreon_build.services.dep_plan.order import topo_layers as _kahn_layers  # noqa: F401
from oreon_build.services.dep_plan.errors import root_coverage_errors  # noqa: F401

__all__ = [
    "BuildPlanResult",
    "PlannedPackage",
    "chain_spec_root_packages",
    "fetch_and_plan_automatic_chain",
    "plan_automatic_chain",
]


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


def uncovered_plan_roots(
    root_packages: list[str],
    *,
    package_plans: dict[str, Any] | None,
    skipped: list[str] | None,
) -> list[str]:
    """Compatibility helper used by build_trigger_bg."""
    covered = set(package_plans or {}) | {s for s in (skipped or []) if s}
    out: list[str] = []
    for r in root_packages or []:
        name = (r or "").strip()
        if name and name not in covered:
            out.append(name)
    return out
