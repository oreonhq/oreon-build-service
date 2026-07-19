from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
_SOLVER_CACHE_TTL_SEC = 900.0


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


def _ensure_dnf():
    try:
        import dnf  # type: ignore

        return dnf
    except ImportError:
        import glob

        for p in sorted(
            glob.glob("/usr/lib64/python3.*/site-packages")
            + glob.glob("/usr/lib/python3.*/site-packages")
        ):
            if p not in sys.path:
                sys.path.append(p)
        import dnf  # type: ignore

        return dnf


def _public_repo_base() -> str:
    from oreon_build.config import get_settings

    raw = (getattr(get_settings(), "r2_public_url", None) or "").strip()
    if not raw:
        raw = "https://repo-us.oreonhq.com/"
    return raw.rstrip("/") + "/"


def _branch_baseurl(releasename: str, branch: str, arch: str) -> str:
    return f"{_public_repo_base()}{releasename}/{branch}/{arch}"


def _safe_rpm_name(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._+-]+", "-", (name or "").strip())
    return (out or "pkg")[:80]


@dataclass
class SolveResult:
    ok: bool
    missing_provides: set[str] = field(default_factory=set)
    broken_binaries: set[str] = field(default_factory=set)
    problems: str = ""

    @property
    def missing(self) -> set[str]:
        return set(self.missing_provides)


def parse_depsolve_missing(text: str) -> set[str]:
    out: set[str] = set()
    blob = text or ""
    for rx in (_RE_NOTHING_PROVIDES, _RE_NO_MATCH, _RE_PACKAGE_REQUIRES):
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


class OreonRepoSolver:
    """dnf/hawkey installability against Oreon repos + optional plan overlay Provides."""

    def __init__(
        self,
        *,
        releasename: str,
        branches: list[str],
        arch: str,
        work_dir: str | None = None,
    ) -> None:
        self.releasename = (releasename or "").strip()
        self.branches = [b for b in branches if b]
        self.arch = (arch or "x86_64").strip()
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
        self._base: Any | None = None
        self._resolve_cache: dict[tuple[str, ...], SolveResult] = {}

    def close(self) -> None:
        self._base = None
        if self._owned_td and self._td and os.path.isdir(self._td):
            shutil.rmtree(self._td, ignore_errors=True)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def set_overlay(self, packages: dict[str, Iterable[str]]) -> None:
        new: dict[str, OverlayPackage] = {}
        for name, provides in packages.items():
            src = (name or "").strip()
            if not src:
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
                sorted((k, tuple(sorted(v.provides))) for k, v in new.items())
            ).encode()
        ).hexdigest()
        if fp == self._overlay_fp and self._base is not None:
            return
        self._overlay_pkgs = new
        self._overlay_fp = fp
        self._resolve_cache.clear()
        self._rebuild_overlay_repo()
        self._base = None

    def reuse_overlay_from(self, other: "OreonRepoSolver") -> None:
        """Reuse another arch solver's overlay repo (same noarch mega RPM)."""
        if self is other:
            return
        if (
            self._overlay_fp == other._overlay_fp
            and self._base is not None
            and self._overlay_pkgs is other._overlay_pkgs
        ):
            return
        self._overlay_pkgs = other._overlay_pkgs
        self._overlay_fp = other._overlay_fp
        self._resolve_cache.clear()
        src = other._overlay_dir.resolve()
        dst = self._overlay_dir
        if dst.exists() or dst.is_symlink():
            if dst.is_symlink():
                dst.unlink()
            else:
                shutil.rmtree(dst, ignore_errors=True)
        try:
            dst.symlink_to(src, target_is_directory=True)
        except OSError:
            shutil.copytree(src, dst)
        self._base = None

    def _rebuild_overlay_repo(self) -> None:
        if self._overlay_dir.exists():
            shutil.rmtree(self._overlay_dir, ignore_errors=True)
        self._overlay_dir.mkdir(parents=True, exist_ok=True)
        if not self._overlay_pkgs:
            subprocess.run(
                ["createrepo_c", str(self._overlay_dir)],
                check=False,
                capture_output=True,
                text=True,
            )
            return
        self._build_mega_overlay_rpm()
        subprocess.run(
            ["createrepo_c", str(self._overlay_dir)],
            check=True,
            capture_output=True,
            text=True,
        )

    def _build_mega_overlay_rpm(self) -> None:
        """One rpmbuild for the whole overlay. N packages was death-slow."""
        top = Path(self._td) / "rpmbuild" / "mega"
        for sub in ("BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS"):
            (top / sub).mkdir(parents=True, exist_ok=True)
        all_provides: set[str] = set()
        obsolete_lines: list[str] = []
        for pkg in sorted(self._overlay_pkgs.values(), key=lambda p: p.source_name):
            all_provides.update(pkg.provides)
            all_provides.add(pkg.source_name)
            obsolete_lines.append(f"Obsoletes:      {pkg.source_name} < 99999")
            all_provides.add(pkg.source_name)
        provide_lines = [f"Provides:       {cap}" for cap in sorted(all_provides) if _overlay_cap_ok(cap)]
        spec = "\n".join(
            [
                "Name:           oreon-plan-overlay",
                "Version:        99999",
                "Release:        0",
                "Summary:        oreon plan overlay virtual package",
                "License:        MIT",
                "BuildArch:      noarch",
                *provide_lines,
                *obsolete_lines,
                "%description",
                "Plan overlay provides for packages selected to rebuild",
                "%files",
                "",
            ]
        )
        spec_path = top / "SPECS" / "oreon-plan-overlay.spec"
        spec_path.write_text(spec, encoding="utf-8")
        proc = subprocess.run(
            [
                "rpmbuild",
                "-bb",
                "--quiet",
                "--define",
                f"_topdir {top}",
                "--define",
                "_build_id_links none",
                "--define",
                "source_date_epoch_from_changelog 0",
                str(spec_path),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            logger.warning(
                "mega overlay rpmbuild failed: %s",
                (proc.stderr or proc.stdout or "")[-800:],
            )
            return
        for rpm in (top / "RPMS").rglob("*.rpm"):
            shutil.copy2(rpm, self._overlay_dir / rpm.name)

    def _make_base(self) -> Any:
        dnf = _ensure_dnf()
        base = dnf.Base()
        base.conf.cachedir = str(self._cache_dir)
        base.conf.installroot = str(self._root_dir)
        base.conf.substitutions["releasever"] = "11"
        base.conf.substitutions["arch"] = self.arch
        base.conf.substitutions["basearch"] = (
            "x86_64" if self.arch == "x86_64" else self.arch
        )
        base.conf.sslverify = True
        base.conf.skip_if_unavailable = True
        for branch in self.branches:
            url = _branch_baseurl(self.releasename, branch, self.arch)
            name = f"oreon-{branch}-{self.arch}".replace("/", "-")
            repo = base.repos.add_new_repo(
                name,
                base.conf,
                baseurl=[url],
                skip_if_unavailable=True,
            )
            repo.enable()
            repo.gpgcheck = False
            repo.repo_gpgcheck = False
            repo.metadata_expire = 0
        if self._overlay_pkgs:
            orepo = base.repos.add_new_repo(
                "oreon-plan-overlay",
                base.conf,
                baseurl=[self._overlay_dir.as_uri()],
                skip_if_unavailable=False,
            )
            orepo.enable()
            orepo.gpgcheck = False
            orepo.repo_gpgcheck = False
            orepo.metadata_expire = 0
            orepo.priority = 1
        base.fill_sack(load_system_repo=False, load_available_repos=True)
        return base

    def _get_base(self) -> Any:
        if self._base is None:
            t0 = time.monotonic()
            self._base = self._make_base()
            logger.info(
                "repo solver sack %s/%s/%s overlay=%s in %.1fs",
                self.releasename,
                "+".join(self.branches),
                self.arch,
                len(self._overlay_pkgs),
                time.monotonic() - t0,
            )
        return self._base

    def resolve(self, requirements: Iterable[str]) -> SolveResult:
        reqs = sorted(
            {
                (r or "").strip()
                for r in requirements
                if (r or "").strip() and "%" not in (r or "")
            }
        )
        if not reqs:
            return SolveResult(ok=True)
        key = (self._overlay_fp, *reqs)
        hit = self._resolve_cache.get(key)
        if hit is not None:
            return hit

        dnf = _ensure_dnf()
        base = self._get_base()
        missing: set[str] = set()
        problems: list[str] = []
        try:
            base.reset(goal=True)
        except Exception:
            self._base = None
            base = self._get_base()

        for req in reqs:
            try:
                base.install(req)
            except dnf.exceptions.PackageNotFoundError as exc:
                missing.add(req)
                problems.append(str(exc))
            except Exception as exc:
                missing.add(req)
                problems.append(f"{type(exc).__name__}: {exc}")

        if missing and len(missing) == len(reqs):
            # every BR itself missing already; no need to resolve
            joined = "\n".join(problems)
            result = SolveResult(
                ok=False,
                missing_provides=missing,
                broken_binaries=parse_depsolve_broken_binaries(joined),
                problems=joined,
            )
            self._resolve_cache[key] = result
            return result

        try:
            base.resolve(allow_erasing=True)
            if missing:
                joined = "\n".join(problems)
                result = SolveResult(
                    ok=False,
                    missing_provides=missing,
                    broken_binaries=parse_depsolve_broken_binaries(joined),
                    problems=joined,
                )
            else:
                result = SolveResult(ok=True)
        except dnf.exceptions.DepsolveError as exc:
            text = str(exc)
            problems.append(text)
            missing |= parse_depsolve_missing(text)
            joined = "\n".join(problems)
            result = SolveResult(
                ok=False,
                missing_provides=missing,
                broken_binaries=parse_depsolve_broken_binaries(joined),
                problems=joined,
            )
        except Exception as exc:
            text = f"{type(exc).__name__}: {exc}"
            problems.append(text)
            missing |= parse_depsolve_missing(text)
            if not missing:
                missing.update(reqs)
            joined = "\n".join(problems)
            result = SolveResult(
                ok=False,
                missing_provides=missing,
                broken_binaries=parse_depsolve_broken_binaries(joined),
                problems=joined,
            )

        self._resolve_cache[key] = result
        return result

    def can_install(self, requirements: Iterable[str]) -> bool:
        return self.resolve(requirements).ok

    def missing_provides(self, requirements: Iterable[str]) -> set[str]:
        return set(self.resolve(requirements).missing_provides)


def is_arch_unavailable_failure(result: SolveResult) -> bool:
    """True when dnf has no package for this arch (ExclusiveArch / missing), not a depsolve hole."""
    if result.ok:
        return False
    text = (result.problems or "").lower()
    if "nothing provides" in text:
        return False
    if "problem:" in text and "needed by" in text:
        return False
    if "none of the providers can be installed" in text:
        return False
    if "no match for argument" in text:
        return True
    if "no matching package to install" in text and "problem:" not in text:
        return True
    return False


class MultiArchRepoSolver:
    """Requirement installable when every arch succeeds or only ExclusiveArch gaps remain."""

    def __init__(self, solvers: list[OreonRepoSolver]) -> None:
        self.solvers = solvers

    def close(self) -> None:
        for s in self.solvers:
            s.close()

    def warm(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        if not self.solvers:
            return
        with ThreadPoolExecutor(max_workers=len(self.solvers)) as pool:
            list(pool.map(lambda s: s._get_base(), self.solvers))

    def set_overlay(self, packages: dict[str, Iterable[str]]) -> None:
        if not self.solvers:
            return
        primary = self.solvers[0]
        primary.set_overlay(packages)
        for s in self.solvers[1:]:
            s.reuse_overlay_from(primary)

    def resolve(self, requirements: Iterable[str]) -> SolveResult:
        reqs = list(requirements)
        if not self.solvers:
            return SolveResult(ok=True)
        if len(self.solvers) == 1:
            return self.solvers[0].resolve(reqs)

        from concurrent.futures import ThreadPoolExecutor, as_completed

        missing: set[str] = set()
        broken: set[str] = set()
        problems: list[str] = []
        ok_count = 0
        hard_fail = False
        soft_fail = False
        with ThreadPoolExecutor(max_workers=len(self.solvers)) as pool:
            futs = {pool.submit(s.resolve, reqs): s for s in self.solvers}
            for fut in as_completed(futs):
                s = futs[fut]
                r = fut.result()
                if r.ok:
                    ok_count += 1
                    continue
                if is_arch_unavailable_failure(r):
                    soft_fail = True
                    continue
                hard_fail = True
                missing |= r.missing_provides
                broken |= r.broken_binaries
                if r.problems:
                    problems.append(f"[{s.arch}] {r.problems}")

        # ExclusiveArch: package exists on some arches, absent on others → ok
        if not hard_fail and ok_count > 0:
            return SolveResult(ok=True)
        if not hard_fail and soft_fail and ok_count == 0:
            # absent on every planned arch → real missing
            return SolveResult(
                ok=False,
                missing_provides=set(str(x).strip() for x in reqs if str(x).strip()),
                problems="No match for argument on any target arch",
            )
        return SolveResult(
            ok=not hard_fail and ok_count > 0,
            missing_provides=missing,
            broken_binaries=broken,
            problems="\n".join(problems),
        )

    def can_install(self, requirements: Iterable[str]) -> bool:
        return self.resolve(requirements).ok

    def missing_provides(self, requirements: Iterable[str]) -> set[str]:
        return set(self.resolve(requirements).missing_provides)


def get_repo_solver(
    releasename: str,
    branch: str,
    architectures: list[str],
    *,
    extra_branches: list[str] | None = None,
) -> MultiArchRepoSolver:
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
    key = (releasename, tuple(branches), tuple(arches))
    now = time.monotonic()
    hit = _SOLVER_CACHE.get(key)
    if hit and now - hit[0] < _SOLVER_CACHE_TTL_SEC:
        return hit[1]
    solvers = [
        OreonRepoSolver(releasename=releasename, branches=branches, arch=a)
        for a in arches
    ]
    multi = MultiArchRepoSolver(solvers)
    _SOLVER_CACHE[key] = (now, multi)
    return multi


def clear_repo_solver_cache() -> None:
    for _ts, solver in list(_SOLVER_CACHE.values()):
        try:
            solver.close()
        except Exception:
            pass
    _SOLVER_CACHE.clear()
