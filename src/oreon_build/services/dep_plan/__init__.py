"""Dependency-resolution primitives shared by the layer engine.

The old three-oracle multi-wave planner (`planner.py`/`expand.py`) is gone -
the layer engine now does one static-BR-only pass to seed, then reactively
evaluates dynamic BRs per-package off real publish events. What's left here
(`providers.py`, `order.py`, `install_oracle.py`, `br_oracle.py`, `types.py`)
are still used directly by `layer_engine.py`.
"""

from __future__ import annotations

from oreon_build.services.dep_plan.types import (
    BrEval,
    PackageNode,
    PlanLayer,
    PlannedPackage,
    BuildPlanResult,
)

__all__ = [
    "BrEval",
    "BuildPlanResult",
    "PackageNode",
    "PlanLayer",
    "PlannedPackage",
]
