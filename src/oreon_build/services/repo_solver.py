from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_RE_NOTHING_PROVIDES = re.compile(
    r"nothing provides\s+(?P<cap>.+?)(?:\s+needed by\s|\s*$)",
    re.IGNORECASE,
)
_RE_NO_MATCH = re.compile(
    r"No match for argument:\s*(?P<cap>.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_RE_NO_MATCHING_PACKAGE = re.compile(
    r"No matching package to install:\s*['\"]?(?P<cap>[^'\"\n]+?)['\"]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RE_PACKAGE_REQUIRES = re.compile(
    r"package\s+\S+(?:\s+from\s+\S+)?\s+requires\s+(?P<cap>.+?),\s+but none of the providers can be installed",
    re.IGNORECASE,
)
_RE_NEEDED_BY_NEVRA = re.compile(
    r"needed by\s+(?P<nevra>\S+?)(?:\s+from\s|\s*$)",
    re.IGNORECASE,
)
_RE_PACKAGE_NEVRA = re.compile(
    r"package\s+(?P<nevra>\S+)(?:\s+from\s+\S+)?\s+requires\s+",
    re.IGNORECASE,
)
_ARCH_SUFFIXES = (
    ".x86_64",
    ".aarch64",
    ".ppc64le",
    ".s390x",
    ".noarch",
    ".i686",
)


def nevra_to_name(nevra: str) -> str:
    s = (nevra or "").strip()
    for arch in _ARCH_SUFFIXES:
        if s.endswith(arch):
            s = s[: -len(arch)]
            break
    m = re.match(r"^(?:([^:]+):)?(.+)-([^-]+-[^-]+)$", s)
    if m:
        return m.group(2)
    if s.count("-") >= 2:
        return s.rsplit("-", 2)[0]
    return s

_SOLVER_CACHE: dict[tuple[Any, ...], tuple[float, "OreonRepoSolver"]] = {}
_SOLVER_CACHE_TTL_SEC = 300.0
# keep both arches warm so dual-arch layers do not thrash-evict each other
_SOLVER_CACHE_MAX = 32
_SOLV_LOCK = threading.RLock()


def _overlay_cap_ok(cap: str) -> bool:
    c = (cap or "").strip()
    if not c or len(c) > 220 or "%" in c:
        return False
    if any(ch in c for ch in ("'", '"', "`", "$", ";", "|", "{", "}", "\\", "\n", "\t")):
        return False
    if c.startswith(("(", "=", "<", ">", "!")):
        return False
    if not re.match(r"^[A-Za-z0-9_./+]", c):
        return False
    if c.lower() in {
        "printf",
        "echo",
        "true",
        "false",
        "test",
        "then",
        "else",
        "fi",
        "grep",
        "sed",
        "awk",
    }:
        return False
    return True


def _public_repo_base() -> str:
    from oreon_build.config import get_settings

    raw = (getattr(get_settings(), "r2_public_url", None) or "").strip()
    if not raw:
        raw = "https://repo-us.oreonhq.com/"
    return raw.rstrip("/") + "/"


def _branch_baseurl(releasename: str, branch: str, arch: str) -> str:
    return f"{_public_repo_base()}{releasename}/{branch}/{arch}"


def branch_baseurl(releasename: str, branch: str, arch: str) -> str:
    return _branch_baseurl(releasename, branch, arch)


def branch_repomd_r2_key(releasename: str, branch: str, arch: str) -> str:
    return f"{releasename}/{branch}/{arch}/repodata/repomd.xml"


def mock_addrepo_urls_for_available_branches(
    releasename: str,
    branches: Iterable[str],
    arch: str,
    *,
    r2_client: Any = None,
) -> list[str]:
    """Public repo URLs for branches that already have repomd in R2.

    Skip empty branches. mock --addrepo 404s and kills bootstrap otherwise.
    """
    from oreon_build.services.r2 import get_r2_client

    r2 = r2_client or get_r2_client()
    out: list[str] = []
    seen: set[str] = set()
    for raw in branches:
        b = (raw or "").strip()
        if not b or b in seen:
            continue
        seen.add(b)
        key = branch_repomd_r2_key(releasename, b, arch)
        try:
            if not r2.object_exists(key):
                continue
        except Exception:
            logger.warning("repomd probe failed for %s, skipping addrepo", key, exc_info=True)
            continue
        out.append(branch_baseurl(releasename, b, arch).rstrip("/") + "/")
    return out


@dataclass
class SolveResult:
    ok: bool
    missing_provides: set[str] = field(default_factory=set)
    broken_binaries: set[str] = field(default_factory=set)
    problems: str = ""
    shadow_sources: set[str] = field(default_factory=set)
    repo_sources: set[str] = field(default_factory=set)

    @property
    def missing(self) -> set[str]:
        return set(self.missing_provides)


def parse_depsolve_missing(text: str) -> set[str]:
    out: set[str] = set()
    blob = text or ""
    for rx in (
        _RE_NOTHING_PROVIDES,
        _RE_NO_MATCH,
        _RE_NO_MATCHING_PACKAGE,
        _RE_PACKAGE_REQUIRES,
    ):
        for m in rx.finditer(blob):
            cap = (m.group("cap") or "").strip().rstrip(".")
            if cap:
                out.add(cap)
    return out


def parse_depsolve_broken_binaries(text: str) -> set[str]:
    """RPM names of packages named as uninstallable / needing a missing provide."""
    out: set[str] = set()
    blob = text or ""
    for rx in (_RE_NEEDED_BY_NEVRA, _RE_PACKAGE_NEVRA):
        for m in rx.finditer(blob):
            name = nevra_to_name(m.group("nevra") or "")
            if name:
                out.add(name)
    return out


@dataclass
class OverlayPackage:
    source_name: str
    provides: frozenset[str]
    requires: frozenset[str] = frozenset()
    epoch: int = 0
    version: str = "0"
    release: str = "0"
    arch: str = "noarch"


class OreonRepoSolver:
    """Installability against Oreon repos + plan overlay Provides via libsolv."""

    def __init__(
        self,
        *,
        releasename: str,
        branches: list[str],
        arch: str,
        work_dir: str | None = None,
        repo_urls: list[tuple[str, str]] | None = None,
    ) -> None:
        self.releasename = (releasename or "").strip()
        self.branches = [b for b in branches if b]
        self.arch = (arch or "x86_64").strip()
        self.repo_urls = [
            (str(n).strip(), str(u).rstrip("/"))
            for n, u in (repo_urls or [])
            if str(n).strip() and str(u).strip()
        ]
        self._td = work_dir or tempfile.mkdtemp(prefix="oreon-solv-")
        self._owned_td = work_dir is None
        self._overlay_dir = Path(self._td) / "overlay"
        self._cache_dir = Path(self._td) / "cache"
        self._root_dir = Path(self._td) / "root"
        self._overlay_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._overlay_pkgs: dict[str, OverlayPackage] = {}
        self._overlay_fp = ""
        self._resolve_cache: dict[tuple[str, ...], SolveResult] = {}
        self._solv_engine: Any | None = None

    def _rpc_spec(self) -> dict[str, Any]:
        return {
            "releasename": self.releasename,
            "branches": list(self.branches),
            "arch": self.arch,
            "repo_urls": [list(x) for x in self.repo_urls],
        }

    def _use_rpc(self) -> bool:
        from oreon_build.services.solv_process import rpc_enabled

        return rpc_enabled()

    def close(self) -> None:
        if self._use_rpc():
            from oreon_build.services.solv_process import rpc_close

            rpc_close(self._rpc_spec())
            return
        with _SOLV_LOCK:
            if self._solv_engine is not None:
                try:
                    self._solv_engine.close()
                except Exception:
                    pass
                self._solv_engine = None
            if self._owned_td and self._td and os.path.isdir(self._td):
                shutil.rmtree(self._td, ignore_errors=True)

    def __del__(self) -> None:
        try:
            eng = self._solv_engine
            self._solv_engine = None
            if eng is not None:
                eng.close()
        except Exception:
            pass

    def _get_solv_engine(self) -> Any:
        if self._solv_engine is not None:
            return self._solv_engine
        from oreon_build.services.solv_engine import SolvArchEngine, solv_available

        if not solv_available():
            raise RuntimeError("python-solv bindings unavailable")
        eng = SolvArchEngine(
            releasename=self.releasename,
            branches=self.branches,
            arch=self.arch,
            repo_urls=self.repo_urls or None,
        )
        eng.load()
        self._solv_engine = eng
        return eng

    def _overlay_to_solvables(self) -> dict[str, Any]:
        from oreon_build.services.solv_engine import OverlaySolvable

        return {
            name: OverlaySolvable(
                source_name=pkg.source_name,
                provides=pkg.provides,
                requires=pkg.requires,
                epoch=pkg.epoch,
                version=pkg.version,
                release=pkg.release,
                arch=pkg.arch,
            )
            for name, pkg in self._overlay_pkgs.items()
        }

    def set_overlay(self, packages: dict[str, Iterable[str] | OverlayPackage]) -> None:
        with _SOLV_LOCK:
            new: dict[str, OverlayPackage] = {}
            for name, provides in packages.items():
                src = (name or "").strip()
                if not src:
                    continue
                if isinstance(provides, OverlayPackage):
                    caps = frozenset(
                        p.strip()
                        for p in provides.provides
                        if p
                        and str(p).strip()
                        and "%" not in str(p)
                        and not str(p).strip().startswith("bundled(")
                        and _overlay_cap_ok(str(p).strip())
                    )
                    if not caps:
                        continue
                    requires = frozenset(
                        str(req).strip()
                        for req in (provides.requires or ())
                        if str(req).strip() and "%" not in str(req)
                    )
                    new[src] = OverlayPackage(
                        source_name=src,
                        provides=caps,
                        requires=requires,
                        epoch=int(provides.epoch or 0),
                        version=(provides.version or "0").strip() or "0",
                        release=(provides.release or "0").strip() or "0",
                        arch=(provides.arch or "noarch").strip() or "noarch",
                    )
                    continue
                caps = frozenset(
                    p.strip()
                    for p in provides
                    if p
                    and str(p).strip()
                    and "%" not in str(p)
                    and not str(p).strip().startswith("bundled(")
                    and _overlay_cap_ok(str(p).strip())
                )
                if not caps:
                    continue
                new[src] = OverlayPackage(source_name=src, provides=caps)
            fp = hashlib.sha256(
                repr(
                    sorted(
                        (
                            k,
                            tuple(sorted(v.provides)),
                            tuple(sorted(v.requires)),
                            v.epoch,
                            v.version,
                            v.release,
                            v.arch,
                        )
                        for k, v in new.items()
                    )
                ).encode()
            ).hexdigest()
            if fp == self._overlay_fp and not self._use_rpc():
                return
            self._overlay_pkgs = new
            self._overlay_fp = fp
            self._resolve_cache.clear()
            use_rpc = self._use_rpc()
            spec = self._rpc_spec() if use_rpc else None
            overlay = new if use_rpc else None
            if not use_rpc:
                eng = self._get_solv_engine()
                eng.set_overlay(self._overlay_to_solvables())
        if use_rpc:
            from oreon_build.services.solv_process import rpc_set_overlay

            rpc_set_overlay(spec, overlay)

    def reuse_overlay_from(self, other: "OreonRepoSolver") -> None:
        with _SOLV_LOCK:
            self._reuse_overlay_from(other)

    def _reuse_overlay_from(self, other: "OreonRepoSolver") -> None:
        """Reuse another arch solver's overlay (libsolv copy)."""
        if self is other:
            return
        if (
            self._overlay_fp == other._overlay_fp
            and self._overlay_pkgs is other._overlay_pkgs
            and self._solv_engine is not None
            and other._solv_engine is not None
        ):
            return
        self._overlay_pkgs = other._overlay_pkgs
        self._overlay_fp = other._overlay_fp
        self._resolve_cache.clear()
        if self._use_rpc():
            from oreon_build.services.solv_process import rpc_set_overlay

            rpc_set_overlay(self._rpc_spec(), dict(self._overlay_pkgs))
            return
        eng = self._get_solv_engine()
        eng.set_overlay(self._overlay_to_solvables())

    def _get_base(self) -> Any:
        if self._use_rpc():
            from oreon_build.services.solv_process import rpc_warm

            rpc_warm(self._rpc_spec())
            return None
        with _SOLV_LOCK:
            eng = self._get_solv_engine()
            eng.warm()
        return eng

    def warm(self) -> None:
        self._get_base()

    def resolve(self, requirements: Iterable[str]) -> SolveResult:
        if self._use_rpc():
            from oreon_build.services.solv_process import rpc_resolve

            return rpc_resolve(self._rpc_spec(), [str(r) for r in requirements])
        with _SOLV_LOCK:
            return self._resolve_locked(requirements)

    def _resolve_locked(self, requirements: Iterable[str]) -> SolveResult:
        from oreon_build.core.rpm_requires import requirement_usable_for_solver

        reqs = sorted(
            {
                usable
                for r in requirements
                if (usable := requirement_usable_for_solver(r or ""))
            }
        )
        if not reqs:
            return SolveResult(ok=True)
        key = (self._overlay_fp, *reqs)
        hit = self._resolve_cache.get(key)
        if hit is not None:
            return hit
        if len(self._resolve_cache) > 8000:
            self._resolve_cache.clear()

        eng = self._get_solv_engine()
        result = eng.resolve(reqs)
        self._resolve_cache[key] = result
        return result

    def has_provider(self, requirement: str) -> bool:
        if self._use_rpc():
            from oreon_build.services.solv_process import rpc_has_provider

            return rpc_has_provider(self._rpc_spec(), requirement)
        with _SOLV_LOCK:
            return bool(self._get_solv_engine().has_provider(requirement))

    def sources_providing(self, requirement: str) -> set[str]:
        if self._use_rpc():
            from oreon_build.services.solv_process import rpc_sources_providing

            return rpc_sources_providing(self._rpc_spec(), requirement)
        with _SOLV_LOCK:
            return set(self._get_solv_engine().sources_providing(requirement))

    def source_rpms_providing(self, requirement: str) -> set[str]:
        if self._use_rpc():
            from oreon_build.services.solv_process import rpc_source_rpms_providing

            return rpc_source_rpms_providing(self._rpc_spec(), requirement)
        with _SOLV_LOCK:
            return set(self._get_solv_engine().source_rpms_providing(requirement))

    def capability_index(self) -> Any:
        with _SOLV_LOCK:
            return self._get_solv_engine().capability_index()

    def can_install(self, requirements: Iterable[str]) -> bool:
        return self.resolve(requirements).ok

    def missing_provides(self, requirements: Iterable[str]) -> set[str]:
        return set(self.resolve(requirements).missing_provides)


def is_arch_unavailable_failure(result: SolveResult) -> bool:
    return False


class MultiArchRepoSolver:
    """Requirement installable when every arch succeeds or only ExclusiveArch gaps remain."""

    def __init__(self, solvers: list[OreonRepoSolver]) -> None:
        self.solvers = solvers

    def close(self) -> None:
        for s in self.solvers:
            s.close()

    def warm(self) -> None:
        for solver in self.solvers:
            solver._get_base()

    def set_overlay(self, packages: dict[str, Iterable[str]]) -> None:
        if not self.solvers:
            return
        primary = self.solvers[0]
        primary.set_overlay(packages)
        for solver in self.solvers[1:]:
            solver.reuse_overlay_from(primary)

    def has_provider(self, requirement: str) -> bool:
        return any(s.has_provider(requirement) for s in self.solvers)

    def sources_providing(self, requirement: str) -> set[str]:
        out: set[str] = set()
        for s in self.solvers:
            out |= s.sources_providing(requirement)
        return out

    def source_rpms_providing(self, requirement: str) -> set[str]:
        out: set[str] = set()
        for s in self.solvers:
            out |= s.source_rpms_providing(requirement)
        return out

    def capability_index(self) -> Any:
        from oreon_build.services.repo_capability_index import (
            RepoCapabilityIndex,
            _merge_arch_indexes,
        )

        if not self.solvers:
            idx = RepoCapabilityIndex()
            idx.finalize_lookups()
            return idx
        parts = [s.capability_index() for s in self.solvers]
        return _merge_arch_indexes(parts)

    def resolve(self, requirements: Iterable[str]) -> SolveResult:
        reqs = list(requirements)
        if not self.solvers:
            return SolveResult(ok=True)
        if len(self.solvers) == 1:
            return self.solvers[0].resolve(reqs)

        missing: set[str] = set()
        broken: set[str] = set()
        problems: list[str] = []
        repo_sources: set[str] = set()
        ok_count = 0
        hard_fail = False
        soft_fail = False
        for solver in self.solvers:
            r = solver.resolve(reqs)
            if r.ok:
                ok_count += 1
                repo_sources |= set(getattr(r, "repo_sources", None) or ())
                continue
            if is_arch_unavailable_failure(r):
                soft_fail = True
                missing |= set(r.missing_provides or ())
                if r.problems:
                    problems.append(f"[{solver.arch}] {r.problems}")
                continue
            hard_fail = True
            missing |= set(r.missing_provides or ())
            broken |= set(r.broken_binaries or ())
            if r.problems:
                problems.append(f"[{solver.arch}] {r.problems}")

        if not hard_fail and not soft_fail and ok_count == len(self.solvers):
            return SolveResult(ok=True, repo_sources=repo_sources)
        return SolveResult(
            ok=False,
            missing_provides=missing,
            broken_binaries=broken,
            problems="\n".join(problems)
            or "Build requirements are unavailable on one or more target architectures",
        )

    def can_install(self, requirements: Iterable[str]) -> bool:
        return self.resolve(requirements).ok

    def missing_provides(self, requirements: Iterable[str]) -> set[str]:
        return set(self.resolve(requirements).missing_provides)


def fedora_provides_repo_urls(arch: str) -> list[tuple[str, str]]:
    from oreon_build.config import get_settings

    s = get_settings()
    ver = int(s.layer_provides_fedora_release)
    root = (s.layer_provides_fedora_base or "").rstrip("/")
    a = "aarch64" if (arch or "").strip() in {"aarch64", "arm64"} else "x86_64"
    return [
        (f"fedora-{ver}-everything", f"{root}/releases/{ver}/Everything/{a}/os"),
        (f"fedora-{ver}-updates", f"{root}/updates/{ver}/Everything/{a}"),
    ]


def get_provides_catalog_solver(
    releasename: str,
    branch: str,
    arch: str,
) -> MultiArchRepoSolver:
    from oreon_build.config import get_settings

    mode = str(get_settings().layer_provides_catalog or "fedora").strip().lower()
    if mode == "oreon":
        from oreon_build.services.repo_capability_index import default_capability_extra_branches

        extra = default_capability_extra_branches(branch)
        return get_repo_solver(releasename, branch, [arch], extra_branches=extra)
    return get_repo_solver(
        f"fedora-catalog-{get_settings().layer_provides_fedora_release}",
        "everything",
        [arch],
        repo_urls=fedora_provides_repo_urls(arch),
    )


def get_repo_solver(
    releasename: str,
    branch: str,
    architectures: list[str],
    *,
    extra_branches: list[str] | None = None,
    repo_urls: list[tuple[str, str]] | None = None,
) -> MultiArchRepoSolver:
    from oreon_build.services.solv_engine import solv_available

    if not solv_available():
        raise RuntimeError(
            "libsolv (python-solv) is required for dependency planning"
        )
    branches: list[str] = []
    for raw in [(branch or "dev").strip(), *((extra_branches or []))]:
        b = (raw or "").strip()
        if b and b not in branches:
            branches.append(b)
    if not branches:
        branches = ["dev"]
    arches = sorted({(a or "").strip() for a in architectures if (a or "").strip()}) or [
        "x86_64"
    ]
    urls = [
        (str(n).strip(), str(u).rstrip("/"))
        for n, u in (repo_urls or [])
        if str(n).strip() and str(u).strip()
    ]
    key = (releasename, tuple(branches), tuple(arches), tuple(urls))
    now = time.monotonic()
    to_close: list[MultiArchRepoSolver] = []
    with _SOLV_LOCK:
        hit = _SOLVER_CACHE.get(key)
        if hit and now - hit[0] < _SOLVER_CACHE_TTL_SEC:
            _SOLVER_CACHE[key] = (now, hit[1])
            return hit[1]

        def _queue_close(sol: MultiArchRepoSolver) -> None:
            to_close.append(sol)

        for k, (ts, sol) in list(_SOLVER_CACHE.items()):
            if now - ts >= _SOLVER_CACHE_TTL_SEC or k == key:
                _SOLVER_CACHE.pop(k, None)
                _queue_close(sol)
        while len(_SOLVER_CACHE) >= _SOLVER_CACHE_MAX:
            oldest_k = min(_SOLVER_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _ts, sol = _SOLVER_CACHE.pop(oldest_k)
            _queue_close(sol)

        solvers = [
            OreonRepoSolver(
                releasename=releasename,
                branches=branches,
                arch=a,
                repo_urls=urls or None,
            )
            for a in arches
        ]
        multi = MultiArchRepoSolver(solvers)
        _SOLVER_CACHE[key] = (now, multi)
    for sol in to_close:
        try:
            sol.close()
        except Exception:
            pass
    multi.warm()
    return multi


def invalidate_repo_solver_cache(
    releasename: str,
    branch: str,
    architectures: Iterable[str] | None = None,
) -> None:
    rel = (releasename or "").strip()
    target_branch = (branch or "").strip()
    arch_set = {
        (arch or "").strip()
        for arch in (architectures or ())
        if (arch or "").strip()
    }
    with _SOLV_LOCK:
        for key, (_timestamp, solver) in list(_SOLVER_CACHE.items()):
            key_rel, key_branches, key_arches = key[0], key[1], key[2]
            if key_rel != rel or target_branch not in key_branches:
                continue
            if arch_set and not arch_set.intersection(key_arches):
                continue
            _SOLVER_CACHE.pop(key, None)
            try:
                solver.close()
            except Exception:
                logger.debug("failed closing invalidated repo solver", exc_info=True)
    try:
        from oreon_build.services.solv_process import rpc_drop_release

        rpc_drop_release(rel)
    except Exception:
        logger.debug("solv worker drop after invalidate failed", exc_info=True)


def clear_repo_solver_cache() -> None:
    for _ts, solver in list(_SOLVER_CACHE.values()):
        try:
            solver.close()
        except Exception:
            pass
    _SOLVER_CACHE.clear()
    import gc

    gc.collect()

