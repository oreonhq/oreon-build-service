"""BR filtering based on RPM rich-dep semantics only. No package name lists."""

from __future__ import annotations

import re
from typing import Iterable

from oreon_build.services.repo_capability_index import is_rich_dependency, is_rpmlib_requirement

# RPM rich dep boolean false: (foo if 0) / (foo if false)
_RE_IF_FALSE = re.compile(r"\bif\s+(0|false)\b", re.IGNORECASE)


def is_vacuous_rich_requirement(requirement: str) -> bool:
    """True for rich deps gated on a constant false (never required)."""
    raw = (requirement or "").strip()
    if not raw:
        return False
    if not (is_rich_dependency(raw) or " if " in raw.lower()):
        return False
    return bool(_RE_IF_FALSE.search(raw))


def filter_plan_brs(brs: Iterable[str]) -> list[str]:
    """BRs the install oracle should resolve.

    Drops rpmlib, macros, and rich deps that are vacuously false (`if 0`).
    Does not invent allow/deny lists of package names.
    """
    from oreon_build.core.rpm_requires import is_chain_plan_requirement, requirement_usable_for_solver

    out: list[str] = []
    seen: set[str] = set()
    for br in brs:
        c = requirement_usable_for_solver(br or "")
        if not c or c in seen:
            continue
        if is_rpmlib_requirement(c):
            continue
        if is_vacuous_rich_requirement(c):
            continue
        if is_rich_dependency(c) or is_chain_plan_requirement(c):
            seen.add(c)
            out.append(c)
            continue
        if c.startswith("/") or ".so" in c or "()(" in c:
            seen.add(c)
            out.append(c)
    return out
