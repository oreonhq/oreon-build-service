"""Repo-aware chain dependency closure (what to build vs use from published repo)."""
from __future__ import annotations

import logging
import os
import re
import subprocess
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

logger = logging.getLogger(__name__)

ReqStatus = Literal["satisfied", "missing", "conflict"]

_RE_BR_LINE = re.compile(
    r"^([A-Za-z0-9_][A-Za-z0-9+._-]*)"
    r"(?:\s*([<>=!]+)\s*([0-9][0-9a-zA-Z._+~^:-]*))?"
    r"\s*$"
)


@dataclass(frozen=True, order=True)
class RpmEvr:
    epoch: int
    version: str
    release: str

    def canonical(self) -> str:
        return f"{self.epoch}:{self.version}-{self.release}"

    @staticmethod
    def from_canonical(s: str) -> RpmEvr:
        raw = (s or "").strip()
        if not raw:
            return RpmEvr(0, "0", "0")
        epoch = 0
        rest = raw
        if ":" in rest:
            ep, rest = rest.split(":", 1)
            try:
                epoch = int(ep.strip() or "0")
            except ValueError:
                epoch = 0
        if "-" in rest:
            ver, rel = rest.split("-", 1)
        else:
            ver, rel = rest, "0"
        return RpmEvr(epoch, ver.strip() or "0", rel.strip() or "0")


@dataclass
class RepoSatisfactionIndex:
    """Published repo metadata for chain skip/conflict checks."""

    provides_index: dict[str, set[str]]
    known_sources: set[str]
    provide_best_evr: dict[str, RpmEvr]
    source_best_evr: dict[str, RpmEvr]


@dataclass
class ChainVersionConflict:
    consumer: str
    requirement: str
    detail: str


@dataclass
class ChainDependencyPlan:
    sources_to_build: set[str] = field(default_factory=set)
    conflicts: list[ChainVersionConflict] = field(default_factory=list)


def _vercmp(a: RpmEvr, b: RpmEvr) -> int:
    """Compare EVRs. Default is a fast lexical/tuple path; set OREON_CHAIN_USE_RPM_VERCMP=1 for rpm vercmp."""
    ta = (a.epoch, a.version, a.release)
    tb = (b.epoch, b.version, b.release)
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    if (os.getenv("OREON_CHAIN_USE_RPM_VERCMP") or "").strip().lower() not in ("1", "true", "yes"):
        return 0
    sa, sb = a.canonical(), b.canonical()
    try:
        proc = subprocess.run(
            ["rpm", "--eval", f"%{{vercmp:{sa},{sb}}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip().lstrip("-").isdigit():
            return int((proc.stdout or "0").strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return 0


def _evr_meets_constraint(installed: RpmEvr, op: str | None, want: RpmEvr) -> bool:
    if not op:
        return True
    op = op.strip()
    c = _vercmp(installed, want)
    if op == ">=":
        return c >= 0
    if op == "<=":
        return c <= 0
    if op == ">":
        return c > 0
    if op == "<":
        return c < 0
    if op == "=" or op == "==":
        return c == 0
    if op == "!=":
        return c != 0
    return True


def _parse_br_requirement(line: str) -> tuple[str, str | None, RpmEvr | None]:
    s = (line or "").strip()
    if not s:
        return "", None, None
    m = _RE_BR_LINE.match(s.split()[0] if " " in s and not _RE_BR_LINE.match(s) else s)
    if not m and " " in s:
        m = _RE_BR_LINE.match(s)
    if not m:
        tok = s.split()[0] if s.split() else ""
        return tok, None, None
    name = m.group(1)
    op = m.group(2)
    ver_tok = m.group(3)
    want = None
    if ver_tok:
        if "-" in ver_tok and ":" not in ver_tok:
            want = RpmEvr(0, ver_tok.split("-", 1)[0], ver_tok.split("-", 1)[1])
        else:
            want = RpmEvr.from_canonical(ver_tok)
    return name, op, want


def _best_provide_evr(repo: RepoSatisfactionIndex, cap: str) -> RpmEvr | None:
    c = (cap or "").strip()
    if not c:
        return None
    best: RpmEvr | None = None
    for key in (c, c.lower()):
        evr = repo.provide_best_evr.get(key)
        if evr is None:
            continue
        if best is None or _vercmp(evr, best) > 0:
            best = evr
    return best


def _requirement_status(
    requirement_line: str,
    repo: RepoSatisfactionIndex,
) -> tuple[ReqStatus, str]:
    name, op, want = _parse_br_requirement(requirement_line)
    if not name:
        return "satisfied", ""
    best = _best_provide_evr(repo, name)
    if best is None:
        if name in repo.provides_index or any(
            k.lower() == name.lower() for k in repo.provides_index
        ):
            return "missing", f"{name} is listed in repo metadata but has no version"
        return "missing", f"{name} is not in the published repo"
    if want is None:
        return "satisfied", ""
    if _evr_meets_constraint(best, op, want):
        return "satisfied", ""
    return (
        "conflict",
        f"repo has {name} at {best.canonical()} but the chain needs {name} {op or ''} {want.canonical()}".strip(),
    )


def source_published_at_least_spec(
    source: str,
    spec_evrs: dict[str, str],
    repo: RepoSatisfactionIndex,
) -> bool:
    """True when published binaries from ``source`` are at or above the distgit spec EVR."""
    spec_raw = spec_evrs.get(source)
    if not spec_raw:
        return False
    spec_evr = RpmEvr.from_canonical(spec_raw)
    repo_evr = repo.source_best_evr.get(source)
    if repo_evr is None:
        return False
    return _vercmp(repo_evr, spec_evr) >= 0


def chain_target_needs_build(
    source: str,
    spec_evrs: dict[str, str],
    repo: RepoSatisfactionIndex,
) -> bool:
    """User-named chain roots rebuild when spec is ahead of the repo or absent from repo."""
    if source not in spec_evrs:
        return True
    return not source_published_at_least_spec(source, spec_evrs, repo)


def sources_needing_chain_build(
    sources: Iterable[str],
    order: Any,
    repo: RepoSatisfactionIndex,
    *,
    consumer_packages: Iterable[str],
) -> set[str]:
    """
  Distgit sources that still need a chain build (not already in repo at spec EVR,
  or still required because a consumer has an unsatisfied BuildRequires on them).
    """
    spec_evrs: dict[str, str] = (
        order.spec_evrs if isinstance(getattr(order, "spec_evrs", None), dict) else {}
    )
    needed: set[str] = set()
    for raw in sources:
        name = str(raw).strip()
        if not name:
            continue
        if not source_published_at_least_spec(name, spec_evrs, repo):
            needed.add(name)
            continue
        if chain_source_still_required_for_consumers(
            name, order, repo, consumer_packages
        ):
            needed.add(name)
    return needed


def chain_source_still_required_for_consumers(
    source: str,
    order: Any,
    repo: RepoSatisfactionIndex,
    consumer_packages: Iterable[str],
) -> bool:
    """
    True when some chain consumer still has an unsatisfied BuildRequires line that
    resolves to ``source`` (e.g. consumer needs foo-devel -> foo).
    """
    br_by_src: dict[str, list[str]] = (
        order.build_requires_raw
        if isinstance(getattr(order, "build_requires_raw", None), dict)
        else {}
    )
    consumers = {str(c).strip() for c in consumer_packages if str(c).strip()}
    if not consumers:
        return True
    prov_cache: dict[str, set[str]] = {}
    for cons in consumers:
        if cons == source:
            continue
        for br_line in br_by_src.get(cons, []):
            br_name, _, _ = _parse_br_requirement(br_line)
            if not br_name:
                continue
            status, _ = _requirement_status(br_line, repo)
            if status == "satisfied":
                continue
            if br_name not in prov_cache:
                prov_cache[br_name] = _providers_for_br(br_name, order, repo, spec_only=True)
            for prov in prov_cache[br_name]:
                if prov == source:
                    return True
    return False


def _providers_for_br(
    br_name: str,
    order: Any,
    repo: RepoSatisfactionIndex,
    *,
    spec_only: bool = False,
) -> set[str]:
    cap_map = order.capability_providers if isinstance(order.capability_providers, dict) else {}
    spec_names = {str(n) for n in (order.package_names or [])}
    found: set[str] = set()
    if br_name in cap_map:
        for src in cap_map[br_name]:
            s = str(src)
            if s in spec_names:
                found.add(s)
    if found or spec_only:
        return found
    from oreon_build.services.distgit_order import resolve_distgit_sources_for_capability

    for src in resolve_distgit_sources_for_capability(
        order,
        br_name,
        repo_provides_index=repo.provides_index,
        repo_known_sources=repo.known_sources,
    ):
        found.add(src)
    return {s for s in found if s in spec_names}


def chain_sources_missing_from_repo_for_consumers(
    consumer_packages: Iterable[str],
    order: Any,
    repo: RepoSatisfactionIndex,
    *,
    spec_names: set[str] | None = None,
) -> set[str]:
    """One pass: distgit sources to build for repo-missing BuildRequires (spec Provides only)."""
    br_by_src: dict[str, list[str]] = (
        order.build_requires_raw
        if isinstance(getattr(order, "build_requires_raw", None), dict)
        else {}
    )
    consumers = {str(c).strip() for c in consumer_packages if str(c).strip()}
    names = spec_names if spec_names is not None else {str(n) for n in (order.package_names or []) if n}
    prov_cache: dict[str, set[str]] = {}
    needed: set[str] = set()
    for cons in consumers:
        for br_line in br_by_src.get(cons, []):
            br_name, _, _ = _parse_br_requirement(br_line)
            if not br_name:
                continue
            status, _ = _requirement_status(br_line, repo)
            if status != "missing":
                continue
            if br_name not in prov_cache:
                prov_cache[br_name] = _providers_for_br(br_name, order, repo, spec_only=True)
            for prov in prov_cache[br_name]:
                if prov != cons and prov in names:
                    needed.add(prov)
    return needed


def compute_chain_dependency_build_set(
    roots: Iterable[str],
    order: Any,
    repo: RepoSatisfactionIndex,
    user_targets: set[str],
) -> ChainDependencyPlan:
    """
    Walk BuildRequires from chain roots and return source packages that must be built.

    Satisfied requirements are taken from the published repo (existence + version).
    Unsatisfied requirements add distgit providers. Version conflicts fail the plan.
    """
    spec_names = {str(n) for n in (order.package_names or []) if n}
    spec_evrs: dict[str, str] = (
        order.spec_evrs if isinstance(getattr(order, "spec_evrs", None), dict) else {}
    )
    br_by_src: dict[str, list[str]] = (
        order.build_requires_raw
        if isinstance(getattr(order, "build_requires_raw", None), dict)
        else {}
    )
    user_lower = {str(u).lower() for u in user_targets if u}

    plan = ChainDependencyPlan()
    queue: deque[str] = deque()
    seen: set[str] = set()

    for r in roots:
        r = str(r).strip()
        if r and r in spec_names:
            queue.append(r)

    while queue:
        cons = queue.popleft()
        if cons in seen:
            continue
        seen.add(cons)

        if cons.lower() in user_lower and chain_target_needs_build(cons, spec_evrs, repo):
            plan.sources_to_build.add(cons)

        for br_line in br_by_src.get(cons, []):
            br_name, _, _ = _parse_br_requirement(br_line)
            if not br_name:
                continue
            status, detail = _requirement_status(br_line, repo)
            if status == "satisfied":
                continue
            if status == "conflict":
                plan.conflicts.append(
                    ChainVersionConflict(consumer=cons, requirement=br_line, detail=detail)
                )
                continue

            for prov_src in sorted(_providers_for_br(br_name, order, repo, spec_only=True)):
                if prov_src == cons or prov_src not in spec_names:
                    continue
                # Repo may ship the base source RPM while this BR names -devel or pkgconfig
                # caps that are still missing. Do not skip just because the provider source
                # is published when the requirement name is different.
                if (
                    prov_src.lower() == br_name.lower()
                    and source_published_at_least_spec(prov_src, spec_evrs, repo)
                ):
                    continue
                if prov_src not in plan.sources_to_build:
                    plan.sources_to_build.add(prov_src)
                    queue.append(prov_src)

    return plan


def format_chain_version_conflict_message(
    conflicts: list[ChainVersionConflict],
    *,
    releasename: str,
    distgit_url: str,
    distgit_branch: str,
) -> str:
    lines = [
        "Chain build stopped: a dependency version in the published repo cannot satisfy "
        "what this chain needs. Fix the spec or repo, then re-trigger the chain.",
        "",
        f"Release: {releasename}",
        f"Specs repo: {distgit_url} (branch {distgit_branch})",
        "",
        "Conflicts:",
    ]
    for c in conflicts:
        lines.append(f"  - {c.consumer} needs {c.requirement}")
        if c.detail:
            lines.append(f"      {c.detail}")
    lines.append("")
    lines.append(
        "If the repo copy is too old, rebuild the provider package or adjust BuildRequires. "
        "If the repo is correct, bump or fix the consumer spec."
    )
    return "\n".join(lines)
