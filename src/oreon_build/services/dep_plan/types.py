from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BrEval:
    buildrequires: list[str]
    complete: bool
    error: str = ""
    pass_kind: str = "final"  # final | pass1_static | cached


@dataclass
class PackageNode:
    name: str
    spec_text: str = ""
    brs: list[str] = field(default_factory=list)
    br_complete: bool = False
    br_evaluated: bool = False
    br_pass_kind: str = ""
    br_error: str = ""
    action: str = "pending"  # pending | build | skip | block
    build_profile: str = "full"
    rpmbuild_defines: dict[str, str] = field(default_factory=dict)
    next_profile: str | None = None
    reason: str = ""
    provides: set[str] = field(default_factory=set)
    exclusive_arches: set[str] | None = None
    has_generate: bool = False


@dataclass
class PlannedPackage:
    name: str
    action: str
    build_profile: str
    rpmbuild_defines: dict[str, str]
    next_profile: str | None = None
    reason: str = ""


@dataclass
class PlanLayer:
    packages: list[PlannedPackage]


@dataclass
class BuildPlanResult:
    releasename: str
    repo_branch: str
    layers: list[list[PlannedPackage]]
    chain_spec: str
    package_plans: dict[str, dict[str, Any]]
    skipped: list[str]
    errors: list[str]
    blocking_errors: list[str] = field(default_factory=list)
    cycle_warning: bool = False

    def __post_init__(self) -> None:
        if self.blocking_errors is None:
            self.blocking_errors = []
