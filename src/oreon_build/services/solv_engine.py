"""libsolv installability engine for Oreon repos + plan overlay.

Loads rpmmd into a persistent Pool, caches .solv on disk, and injects planned
packages as real solvables (with EVR) instead of rpmbuild+createrepo each time.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import lzma
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from oreon_build.core.rpm_requires import parse_versioned_requirement

logger = logging.getLogger(__name__)

_RE_REQUESTED = re.compile(
    r"nothing provides requested\s+(?P<cap>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RE_NOTHING_PROVIDES = re.compile(
    r"nothing provides\s+(?P<cap>.+?)(?:\s+needed by\s|\s*$)",
    re.IGNORECASE,
)
_RE_NO_MATCH = re.compile(
    r"No match for argument:\s*(?P<cap>.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_REPOMD_NS = {"r": "http://linux.duke.edu/metadata/repo"}


def _ensure_solv():
    try:
        import solv  # type: ignore

        return solv
    except ImportError:
        import glob
        import sys

        for p in sorted(
            glob.glob("/usr/lib64/python3.*/site-packages")
            + glob.glob("/usr/lib/python3.*/site-packages")
        ):
            if p not in sys.path:
                sys.path.append(p)
        import solv  # type: ignore

        return solv


def solv_available() -> bool:
    try:
        _ensure_solv()
        return True
    except Exception:
        return False


def _solv_cache_root() -> Path:
    raw = (os.environ.get("OREON_SOLV_CACHE_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".oreon" / "solv-cache"


def _public_repo_base() -> str:
    from oreon_build.config import get_settings

    raw = (getattr(get_settings(), "r2_public_url", None) or "").strip()
    if not raw:
        raw = "https://repo-us.oreonhq.com/"
    return raw.rstrip("/") + "/"


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
    return True


def _http_get(url: str, dest: Path, *, timeout: int = 180) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "oreon-build-service/solv-engine"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    tmp.replace(dest)


def _decompress_metadata(src: Path, dest: Path) -> None:
    name = src.name.lower()
    data = src.read_bytes()
    if name.endswith(".zst") or data[:4] == b"\x28\xb5\x2f\xfd":
        import zstandard as zstd
        import io

        dctx = zstd.ZstdDecompressor()
        try:
            dest.write_bytes(dctx.decompress(data))
        except zstd.ZstdError:
            with open(src, "rb") as f, open(dest, "wb") as out:
                dctx.copy_stream(f, out)
        return
    if name.endswith(".xz") or data[:6] == b"\xfd7zXZ\x00":
        dest.write_bytes(lzma.decompress(data))
        return
    if name.endswith(".gz") or data[:2] == b"\x1f\x8b":
        dest.write_bytes(gzip.decompress(data))
        return
    dest.write_bytes(data)


def _rel_flags(solv: Any, op: str) -> int:
    eq = solv.REL_EQ
    gt = solv.REL_GT
    lt = solv.REL_LT
    if op in ("=", "=="):
        return eq
    if op == ">":
        return gt
    if op == ">=":
        return gt | eq
    if op == "<":
        return lt
    if op == "<=":
        return lt | eq
    return eq


def _req_to_dep_id(pool: Any, solv: Any, requirement: str) -> int | None:
    raw = (requirement or "").strip()
    if not raw or "%" in raw:
        return None
    # rich deps like (crate(foo) >= 1 with crate(foo) < 2~) need librpm parse
    if raw.startswith("(") and raw.endswith(")"):
        try:
            dep = pool.parserpmrichdep(raw)
        except Exception:
            dep = None
        if dep is None:
            return None
        dep_id = getattr(dep, "id", None)
        return int(dep_id) if dep_id is not None else None
    name, op, ver = parse_versioned_requirement(raw)
    if not name:
        return None
    if not op or not ver:
        return int(pool.str2id(name, True))
    return int(
        pool.rel2id(
            pool.str2id(name, True),
            pool.str2id(ver, True),
            _rel_flags(solv, op),
        )
    )


def _parse_missing_from_problems(texts: Iterable[str]) -> set[str]:
    blob = "\n".join(texts)
    out: set[str] = set()
    for m in _RE_REQUESTED.finditer(blob):
        cap = (m.group("cap") or "").strip().rstrip(".")
        if cap:
            out.add(cap)
    for rx in (_RE_NO_MATCH, _RE_NOTHING_PROVIDES):
        for m in rx.finditer(blob):
            cap = (m.group("cap") or "").strip().rstrip(".")
            if not cap:
                continue
            if cap.lower().startswith("requested "):
                cap = cap[10:].strip()
            out.add(cap)
    return out


def _dep_to_cap(dep: Any) -> str:
    if dep is None:
        return ""
    raw = str(dep).strip()
    if not raw:
        return ""
    if raw.lower().startswith("requested "):
        raw = raw[10:].strip()
    # keep rich deps intact. stripping " >= " turns
    # (crate(foo) >= 1 with crate(foo) < 2) into "(crate(foo)" which is garbage
    if raw.startswith("(") and raw.endswith(")"):
        return raw.rstrip(".")
    # versioned provide/require text like "foo = 1-1" → primary name
    for sep in (" = ", " >= ", " <= ", " > ", " < ", " == "):
        if sep in raw:
            raw = raw.split(sep, 1)[0].strip()
            break
    return raw.rstrip(".")


def _missing_from_solver_problems(problems: Iterable[Any], solv: Any) -> set[str]:
    """Extract missing caps from libsolv Problem/Ruleinfo. Never invents extras."""
    out: set[str] = set()
    job_nothing = solv.Solver.SOLVER_RULE_JOB_NOTHING_PROVIDES_DEP
    job_unknown = solv.Solver.SOLVER_RULE_JOB_UNKNOWN_PACKAGE
    pkg_nothing = solv.Solver.SOLVER_RULE_PKG_NOTHING_PROVIDES_DEP
    interesting = {job_nothing, job_unknown, pkg_nothing}
    texts: list[str] = []
    for p in problems or ():
        texts.append(str(p))
        try:
            rules = p.findallproblemrules()
        except Exception:
            continue
        for rule in rules or ():
            try:
                info = rule.info()
            except Exception:
                info = None
            if info is None:
                continue
            rtype = getattr(info, "type", None)
            if rtype is None:
                rtype = getattr(rule, "type", None)
            if rtype not in interesting:
                # still take dep when problemstr says nothing provides
                pstr = ""
                try:
                    pstr = (info.problemstr() or "").lower()
                except Exception:
                    pstr = str(info).lower()
                if "nothing provides" not in pstr and "no match" not in pstr:
                    continue
            cap = _dep_to_cap(getattr(info, "dep", None))
            if cap:
                out.add(cap)
    out |= _parse_missing_from_problems(texts)
    return out


def _source_name_from_solvable(sol: Any, solv: Any) -> str:
    src = ""
    try:
        src = (sol.lookup_str(solv.SOLVABLE_SOURCENAME) or "").strip()
    except Exception:
        src = ""
    if src:
        return src
    try:
        pkg = sol.lookup_sourcepkg()
    except Exception:
        pkg = None
    if pkg:
        raw = str(pkg).strip()
        if raw.endswith(".src.rpm"):
            raw = raw[:-8]
        # N-V-R.src → drop last two dash fields when possible
        parts = raw.rsplit("-", 2)
        if len(parts) == 3:
            return parts[0]
        return raw
    return (getattr(sol, "name", None) or "").strip()


@dataclass
class _RepoMetaFiles:
    checksum: str
    primary_xml: Path
    filelists_xml: Path | None
    solv_path: Path


@dataclass
class OverlaySolvable:
    source_name: str
    provides: frozenset[str]
    epoch: int = 0
    version: str = "0"
    release: str = "0"
    arch: str = "noarch"


class SolvArchEngine:
    """One-arch libsolv pool over Oreon branch repos + plan overlay."""

    def __init__(
        self,
        *,
        releasename: str,
        branches: list[str],
        arch: str,
        cache_root: Path | None = None,
    ) -> None:
        self.releasename = (releasename or "").strip()
        self.branches = [b for b in branches if (b or "").strip()]
        self.arch = (arch or "x86_64").strip()
        self.cache_root = cache_root or _solv_cache_root()
        self._solv = _ensure_solv()
        self._pool: Any | None = None
        self._overlay_repo: Any | None = None
        self._overlay_fp = ""
        self._overlay_pkgs: dict[str, OverlaySolvable] = {}
        self._resolve_cache: dict[tuple[str, ...], Any] = {}
        self._loaded = False

    def close(self) -> None:
        self._pool = None
        self._overlay_repo = None
        self._loaded = False
        self._resolve_cache.clear()

    def load(self) -> None:
        if self._loaded and self._pool is not None:
            return
        t0 = time.monotonic()
        solv = self._solv
        pool = solv.Pool()
        pool.setarch(self.arch)
        for branch in self.branches:
            meta = self._ensure_branch_meta(branch)
            repo = pool.add_repo(f"oreon-{branch}-{self.arch}".replace("/", "-"))
            if meta.solv_path.exists() and meta.solv_path.stat().st_size > 64:
                repo.add_solv(str(meta.solv_path))
            else:
                self._fill_repo_from_xml(repo, meta)
                self._write_solv(repo, meta.solv_path)
        pool.addfileprovides()
        pool.createwhatprovides()
        self._pool = pool
        self._overlay_repo = pool.add_repo("oreon-plan-overlay")
        self._loaded = True
        logger.info(
            "solv engine ready %s/%s/%s branches=%s in %.2fs",
            self.releasename,
            "+".join(self.branches),
            self.arch,
            len(self.branches),
            time.monotonic() - t0,
        )

    def warm(self) -> None:
        self.load()

    def set_overlay(self, packages: dict[str, OverlaySolvable]) -> None:
        self.load()
        fp = hashlib.sha256(
            repr(
                sorted(
                    (
                        k,
                        tuple(sorted(v.provides)),
                        v.epoch,
                        v.version,
                        v.release,
                        v.arch,
                    )
                    for k, v in packages.items()
                )
            ).encode()
        ).hexdigest()
        if fp == self._overlay_fp and self._overlay_repo is not None:
            return
        self._overlay_pkgs = dict(packages)
        self._overlay_fp = fp
        self._resolve_cache.clear()
        self._rebuild_overlay()

    def _iter_providers(self, requirement: str) -> list[Any]:
        self.load()
        assert self._pool is not None
        solv = self._solv
        pool = self._pool
        dep_id = _req_to_dep_id(pool, solv, requirement)
        if dep_id is None:
            return []
        try:
            found = pool.whatprovides(dep_id)
        except Exception:
            return []
        out: list[Any] = []
        for item in found or ():
            if item is None:
                continue
            # whatprovides may yield solvables directly
            if hasattr(item, "name"):
                out.append(item)
                continue
            try:
                out.append(pool.id2solvable(int(item)))
            except Exception:
                continue
        return [s for s in out if s is not None]

    def has_provider(self, requirement: str) -> bool:
        return bool(self._iter_providers(requirement))

    def providers_for(self, requirement: str) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for sol in self._iter_providers(requirement):
            n = (getattr(sol, "name", None) or "").strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)
        return names

    def sources_providing(self, requirement: str) -> set[str]:
        out: set[str] = set()
        for sol in self._iter_providers(requirement):
            src = _source_name_from_solvable(sol, self._solv)
            if src:
                out.add(src)
            name = (getattr(sol, "name", None) or "").strip()
            if name:
                out.add(name)
        return out

    def capability_index(self) -> Any:
        """Build RepoCapabilityIndex from loaded rpmmd pool (no R2 re-parse)."""
        from oreon_build.services.repo_capability_index import RepoCapabilityIndex

        self.load()
        assert self._pool is not None
        solv = self._solv
        pool = self._pool
        index = RepoCapabilityIndex()
        overlay_name = "oreon-plan-overlay"
        for sol in pool.solvables_iter():
            try:
                repo = sol.repo
                rname = (getattr(repo, "name", None) or "").strip()
            except Exception:
                rname = ""
            if rname == overlay_name:
                continue
            name = (getattr(sol, "name", None) or "").strip()
            if not name:
                continue
            src = _source_name_from_solvable(sol, solv) or name
            index.binary_names.add(name)
            index.source_names.add(src)
            index.binary_source[name] = src
            index.provides.add(name)
            index.capability_providers.setdefault(name, set()).add(src)
            for dep in sol.lookup_deparray(solv.SOLVABLE_PROVIDES) or ():
                cap = _dep_to_cap(dep)
                if not cap:
                    continue
                index.provides.add(cap)
                index.capability_providers.setdefault(cap, set()).add(src)
            reqs: set[str] = set()
            for dep in sol.lookup_deparray(solv.SOLVABLE_REQUIRES) or ():
                cap = _dep_to_cap(dep)
                if cap:
                    reqs.add(cap)
            if reqs:
                index.binary_requires[name] = reqs
        index.source_names_any = set(index.source_names)
        index.binary_names_any = set(index.binary_names)
        index.finalize_lookups()
        return index

    def resolve(self, requirements: Iterable[str]) -> Any:
        from oreon_build.services.repo_solver import (
            SolveResult,
            parse_depsolve_broken_binaries,
        )

        self.load()
        assert self._pool is not None
        solv = self._solv
        pool = self._pool
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
        if len(self._resolve_cache) > 8000:
            self._resolve_cache.clear()

        jobs = []
        missing: set[str] = set()
        for req in reqs:
            dep_id = _req_to_dep_id(pool, solv, req)
            if dep_id is None:
                missing.add(req)
                continue
            jobs.append(
                pool.Job(
                    solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_SOLVABLE_PROVIDES,
                    dep_id,
                )
            )
        if missing and not jobs:
            result = SolveResult(
                ok=False,
                missing_provides=missing,
                problems="\n".join(
                    f"No match for argument: {m}" for m in sorted(missing)
                ),
            )
            self._resolve_cache[key] = result
            return result

        solver = pool.Solver()
        try:
            solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)
        except Exception:
            pass
        problems = solver.solve(jobs)
        if not problems and not missing:
            result = SolveResult(ok=True)
            self._resolve_cache[key] = result
            return result

        texts = [str(p) for p in (problems or ())]
        if missing:
            texts.extend(f"No match for argument: {m}" for m in sorted(missing))
        miss = set(missing)
        if problems:
            miss |= _missing_from_solver_problems(problems, solv)
        # never invent miss=all reqs. empty miss with problems = broken txn, not import spam
        joined = "\n".join(texts)
        result = SolveResult(
            ok=False,
            missing_provides=miss,
            broken_binaries=parse_depsolve_broken_binaries(joined),
            problems=joined,
        )
        self._resolve_cache[key] = result
        return result

    def _rebuild_overlay(self) -> None:
        assert self._pool is not None
        solv = self._solv
        pool = self._pool
        # python-solv has no Pool.free_repo. empty() the same repo or we leak
        # overlay solvables forever and resolve() keeps lying.
        if self._overlay_repo is None:
            self._overlay_repo = pool.add_repo("oreon-plan-overlay")
        else:
            self._overlay_repo.empty()
        repo = self._overlay_repo

        for pkg in sorted(self._overlay_pkgs.values(), key=lambda p: p.source_name):
            s = repo.add_solvable()
            s.name = pkg.source_name
            epoch = int(pkg.epoch or 0)
            ver = (pkg.version or "0").strip() or "0"
            rel = (pkg.release or "0").strip() or "0"
            if "%" in ver or "%" in rel:
                ver, rel = "0", "0"
            s.evr = f"{epoch}:{ver}-{rel}" if epoch else f"{ver}-{rel}"
            s.arch = (pkg.arch or "noarch").strip() or "noarch"
            self_dep = pool.rel2id(
                pool.str2id(pkg.source_name, True),
                pool.str2id(s.evr, True),
                solv.REL_EQ,
            )
            s.add_deparray(solv.SOLVABLE_PROVIDES, self_dep)
            s.add_deparray(solv.SOLVABLE_PROVIDES, pool.str2id(pkg.source_name, True))
            for cap in sorted(pkg.provides):
                c = (cap or "").strip()
                if not c or not _overlay_cap_ok(c) or c == pkg.source_name:
                    continue
                s.add_deparray(solv.SOLVABLE_PROVIDES, pool.str2id(c, True))
                s.add_deparray(
                    solv.SOLVABLE_PROVIDES,
                    pool.rel2id(
                        pool.str2id(c, True),
                        pool.str2id(s.evr, True),
                        solv.REL_EQ,
                    ),
                )
        pool.createwhatprovides()

    def _ensure_branch_meta(self, branch: str) -> _RepoMetaFiles:
        arch_dir = self.cache_root / self.releasename / branch / self.arch
        arch_dir.mkdir(parents=True, exist_ok=True)
        base = f"{_public_repo_base()}{self.releasename}/{branch}/{self.arch}"
        repomd_path = arch_dir / "repomd.xml"
        try:
            _http_get(f"{base}/repodata/repomd.xml", repomd_path)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                empty = _RepoMetaFiles(
                    checksum="empty",
                    primary_xml=arch_dir / "primary-empty.xml",
                    filelists_xml=None,
                    solv_path=arch_dir / "repo.empty.solv",
                )
                if not empty.primary_xml.exists():
                    empty.primary_xml.write_text(
                        '<?xml version="1.0"?>'
                        '<metadata xmlns="http://linux.duke.edu/metadata/common" packages="0"/>\n',
                        encoding="utf-8",
                    )
                return empty
            raise

        root = ET.parse(repomd_path).getroot()
        primary_href = None
        primary_sum = "unknown"
        filelists_href = None
        filelists_sum = None
        for data in root.findall("r:data", _REPOMD_NS):
            dtype = (data.get("type") or "").strip()
            loc = data.find("r:location", _REPOMD_NS)
            href = loc.get("href") if loc is not None else None
            chk = data.find("r:checksum", _REPOMD_NS)
            digest = (chk.text or "").strip() if chk is not None else ""
            if dtype == "primary" and href:
                primary_href = href
                primary_sum = digest or hashlib.sha256(href.encode()).hexdigest()[:16]
            elif dtype == "filelists" and href:
                filelists_href = href
                filelists_sum = digest

        if not primary_href:
            raise RuntimeError(f"no primary in repomd for {base}")

        checksum = primary_sum
        if filelists_sum:
            checksum = hashlib.sha256(
                f"{primary_sum}:{filelists_sum}".encode()
            ).hexdigest()[:40]

        primary_dl = arch_dir / Path(primary_href).name
        primary_xml = arch_dir / f"primary.{checksum}.xml"
        if not primary_xml.exists():
            _http_get(f"{base}/{primary_href}", primary_dl)
            _decompress_metadata(primary_dl, primary_xml)

        filelists_xml = None
        if filelists_href:
            fl_dl = arch_dir / Path(filelists_href).name
            fl_xml = arch_dir / f"filelists.{checksum}.xml"
            if not fl_xml.exists():
                try:
                    _http_get(f"{base}/{filelists_href}", fl_dl)
                    _decompress_metadata(fl_dl, fl_xml)
                    filelists_xml = fl_xml
                except Exception:
                    logger.warning(
                        "filelists fetch failed for %s/%s/%s",
                        self.releasename,
                        branch,
                        self.arch,
                        exc_info=True,
                    )
            else:
                filelists_xml = fl_xml

        return _RepoMetaFiles(
            checksum=checksum,
            primary_xml=primary_xml,
            filelists_xml=filelists_xml,
            solv_path=arch_dir / f"repo.{checksum}.solv",
        )

    def _fill_repo_from_xml(self, repo: Any, meta: _RepoMetaFiles) -> None:
        solv = self._solv
        f = solv.xfopen(str(meta.primary_xml))
        if not repo.add_rpmmd(f, None):
            raise RuntimeError(f"add_rpmmd failed for {meta.primary_xml}")
        if meta.filelists_xml and meta.filelists_xml.exists():
            ff = solv.xfopen(str(meta.filelists_xml))
            try:
                repo.add_rpmmd(ff, "FL")
            except Exception:
                logger.warning(
                    "filelists add_rpmmd failed for %s",
                    meta.filelists_xml,
                    exc_info=True,
                )

    def _write_solv(self, repo: Any, path: Path) -> None:
        solv = self._solv
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        wf = solv.xfopen(str(tmp), "w")
        repo.write(wf)
        wf.close()
        tmp.replace(path)
