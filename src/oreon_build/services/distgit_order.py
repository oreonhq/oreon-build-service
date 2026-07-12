from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import tarfile
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import lru_cache, partial
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

try:
    from specfile import Specfile  # noqa: F401 — optional, used for deep parse
except ImportError:
    Specfile = None  # type: ignore[assignment, misc]

from oreon_build.config import get_settings
from oreon_build.core.rpm_requires import (
    add_capability_providers,
    is_chain_plan_requirement,
    is_distgit_source_name,
    index_provider_map,
    merge_provider_maps,
    requirement_check_capabilities,
    rpm_requirement_capabilities_from_value,
    rpm_requirement_primary,
    rpm_requirement_tokens_from_value,
    sources_for_requirement,
    _pkgconfig_glob_match,
    _providers_from_maps,
)
from oreon_build.core.spec_bcond import (
    active_build_brs,
    default_build_defines,
    default_distro_macros,
    parse_spec_bconds,
)
from oreon_build.services.r2 import get_r2_client

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _distgit_spec_parse_executor() -> ThreadPoolExecutor:
    raw = (os.getenv("OREON_CHAIN_PREPARE_WORKERS") or "32").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 32
    return ThreadPoolExecutor(max_workers=max(4, min(n, 64)))

_BR_NAME = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9+._-]*)")


@dataclass
class DistgitBuildOrderResult:
    releasename: str
    distgit_url: str
    distgit_branch: str
    package_names: list[str]
    edges: list[dict[str, str]]
    layers: list[list[str]]
    capability_providers: dict[str, list[str]]
    spec_evrs: dict[str, str]
    build_requires_raw: dict[str, list[str]]
    errors: list[str]
    cycle_warning: bool


@dataclass
class SpecTreeResolveResult:
    releasename: str
    repo_branch: str
    distgit_url: str
    distgit_branch: str
    mode: str
    total_spec_packages: int
    built_sources: list[str]
    never_built_sources: list[str]
    selected_for_chain: list[str]
    required_missing_sources: list[str]
    edges: list[dict[str, str]]
    layers: list[list[str]]
    chain_string: str
    unresolved_requirements: list[dict[str, str]]
    errors: list[str]
    cycle_warning: bool


@dataclass
class UnbuiltSpecListResult:
    releasename: str
    distgit_url: str
    distgit_branch: str
    total_spec_packages: int
    total_db_packages: int
    unbuilt_packages: list[str]
    errors: list[str]


@dataclass
class _SpecParseResult:
    source_name: str
    build_requires: list[str]
    provided_binary_names: set[str]
    build_requires_raw: list[str] = None  # type: ignore[assignment]
    requires: list[str] = None  # type: ignore[assignment]
    requires_raw: list[str] = None  # type: ignore[assignment]
    epoch: int = 0
    version: str = ""
    release: str = ""

    def __post_init__(self) -> None:
        if self.build_requires_raw is None:
            self.build_requires_raw = list(self.build_requires)
        if self.requires is None:
            self.requires = []
        if self.requires_raw is None:
            self.requires_raw = list(self.requires)

    def dependency_names(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for name in list(self.build_requires) + list(self.requires):
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def evr_canonical(self) -> str:
        ver = (self.version or "0").strip() or "0"
        rel = (self.release or "0").strip() or "0"
        return db_safe_spec_evr(f"{int(self.epoch)}:{ver}-{rel}")


def db_safe_spec_evr(evr: str, *, max_len: int = 64) -> str:
    s = (evr or "").strip() or "0:0-0"
    if (
        len(s) <= max_len
        and "%" not in s
        and "$(" not in s
        and "${" not in s
    ):
        return s
    digest = hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"0:unexpanded-{digest}"


def _github_owner_repo_from_git_url(url: str) -> tuple[str, str] | None:
    s = (url or "").split("#", 1)[0].strip().rstrip("/")
    if s.lower().endswith(".git"):
        s = s[:-4]
    p = urlparse(s)
    if p.netloc not in ("github.com", "www.github.com") or not p.path:
        return None
    path = p.path.strip("/")
    parts = [x for x in path.split("/") if x]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


_RE_RPM_MACRO = re.compile(r"%\{([^}]+)\}")
_RE_GLOBAL_LINE = re.compile(r"^%\s*global\s+(\S+)\s+(.+)$", re.IGNORECASE)
_RE_GLOBAL_IN_CONDITIONAL = re.compile(r"%global\s+(\S+)\s+([^}%]+)", re.IGNORECASE)
_RE_PC_FILE = re.compile(r"([^/\s]+)\.pc\b")
_RE_SO_FILE = re.compile(r"(lib[A-Za-z0-9_+.-]+)\.so(?:\.\*|(?:\.\d+)*)?")
_RE_FILES_KEEP = re.compile(
    r"^%(?:if\b|ifarch\b|ifnarch\b|else\b|endif\b|elif\b|license\b|doc\b|config\b|"
    r"attr\b|defattr\b|ghost\b|verify\b|dir\b|exclude\b|readme\b|caps\b|missingok\b|files\b)",
    re.IGNORECASE,
)
_RE_SPEC_SECTION = re.compile(
    r"^%(?:package|description|prep|build|install|check|clean|changelog|"
    r"pre\b|post\b|preun\b|postun\b|trigger)",
    re.IGNORECASE,
)


def _files_section_continues(line: str, *, in_files: bool) -> bool | None:
    """Return True/False to set in_files, or None to leave unchanged."""
    low = (line or "").strip().lower()
    if not low.startswith("%"):
        return None
    if low.startswith("%files"):
        return True
    if not in_files:
        return None
    if _RE_FILES_KEEP.match(low):
        return True
    if _RE_SPEC_SECTION.match(low):
        return False
    return None



def _parse_spec_macro_table(spec_text: str) -> dict[str, str]:
    macros: dict[str, str] = {}
    for line in (spec_text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _RE_GLOBAL_LINE.match(s)
        if m:
            macros[m.group(1)] = m.group(2).strip()
            continue
        if "%{?" in s and "%global" in s.lower():
            for gm in _RE_GLOBAL_IN_CONDITIONAL.finditer(s):
                macros[gm.group(1)] = gm.group(2).strip()
    return macros


def _expand_rpm_macros(value: str, macros: dict[str, str], *, max_rounds: int = 24) -> str:
    out = value or ""
    for _ in range(max_rounds):
        changed = False

        def repl(match: re.Match[str]) -> str:
            nonlocal changed
            key = match.group(1)
            if key in macros:
                changed = True
                return macros[key]
            return match.group(0)

        new = _RE_RPM_MACRO.sub(repl, out)
        if not changed:
            break
        out = new
    return out


def _extract_br_tokens_from_tag_value(value: str) -> list[str]:
    return rpm_requirement_capabilities_from_value(value)


def dependency_capabilities(parsed: _SpecParseResult) -> list[str]:
    caps: list[str] = []
    seen: set[str] = set()

    def add(cap: str) -> None:
        c = (cap or "").strip()
        if not c or c in seen or "%" in c:
            return
        seen.add(c)
        caps.append(c)

    for raw in list(parsed.build_requires_raw or []) + list(parsed.requires_raw or []):
        for cap in rpm_requirement_tokens_from_value(raw):
            add(cap)
    for name in parsed.dependency_names():
        add(name)
    return caps


def iter_plan_capabilities(
    parsed: _SpecParseResult,
    bcond: Any,
    repo: Any,
) -> list[str]:
    defines = default_build_defines(bcond)
    caps: list[str] = []
    seen: set[str] = set()

    def add(cap: str) -> None:
        c = (cap or "").strip()
        if not c or c in seen or "%" in c:
            return
        seen.add(c)
        caps.append(c)

    def add_requirement(cap: str) -> None:
        c = (cap or "").strip()
        if not c:
            return
        from oreon_build.services.repo_capability_index import is_rich_dependency

        if is_rich_dependency(c):
            if repo.satisfies_requirement(c):
                return
            missing_fn = getattr(repo, "rich_dependency_missing_leaves", None)
            if callable(missing_fn):
                for leaf in missing_fn(c):
                    if is_chain_plan_requirement(leaf):
                        add(leaf)
                return
            return
        if is_chain_plan_requirement(c):
            add(c)

    seeds: list[str] = []
    for br in active_build_brs(bcond, defines):
        seeds.append(br)

    for cap in seeds:
        add_requirement(cap)
        primary = rpm_requirement_primary(cap) or cap
        walk_closure = repo.satisfies_requirement(cap)
        if not walk_closure:
            any_arch = getattr(repo, "has_capability_any_arch", None)
            if callable(any_arch) and any_arch(primary):
                walk_closure = True
        if walk_closure:
            for miss in repo.transitive_unsatisfied(cap):
                if is_chain_plan_requirement(miss):
                    add(miss)
    return caps


def _add_capability_tokens(target: set[str], raw: str, macros: dict[str, str]) -> None:
    expanded = _expand_rpm_macros(raw, macros).strip()
    if not expanded or "%" in expanded:
        return
    for tok in _extract_br_tokens_from_tag_value(expanded):
        if tok and "%" not in tok:
            target.add(tok)


def _extract_pkg_name_from_package_options(opts: str, source_name: str) -> str | None:
    raw = (opts or "").strip()
    if not raw:
        return None
    if raw.startswith("-n "):
        explicit = raw[3:].strip().split()[0] if raw[3:].strip() else ""
        return explicit or None
    suffix = raw.split()[0]
    if not suffix:
        return None
    if suffix.startswith("-"):
        return None
    return f"{source_name}-{suffix}"



def _spec_parse_fn(releasename: str = "", target_arches: set[str] | None = None):
    macros = default_distro_macros(releasename)
    arches = frozenset(a.lower() for a in (target_arches or set()) if a)
    return partial(_parse_spec_text, distro_macros=macros, target_arches=arches)

def _parse_spec_text(
    spec_text: str,
    filename_hint: str = "inline.spec",
    *,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | frozenset[str] | None = None,
) -> _SpecParseResult:
    br_out: list[str] = []
    br_raw: list[str] = []
    br_seen: set[str] = set()
    req_out: list[str] = []
    req_raw: list[str] = []
    req_seen: set[str] = set()
    provided: set[str] = set()
    source_name = Path(filename_hint).stem
    epoch = 0
    version = ""
    release = ""
    macros = dict(distro_macros or {})
    macros.update(_parse_spec_macro_table(spec_text))
    arches = {a.lower() for a in (target_arches or set()) if a}
    bcond = parse_spec_bconds(spec_text, distro_macros=distro_macros, target_arches=arches or None)
    macros.update(bcond.macros)
    for line in (spec_text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        us = s.upper()
        if us.startswith("NAME:"):
            val = _expand_rpm_macros(s.split(":", 1)[1].strip(), macros)
            if val and "%" not in val:
                source_name = val
                macros["name"] = val
            continue
        if us.startswith("%PACKAGE "):
            opts = s.split(None, 1)[1].strip() if len(s.split(None, 1)) > 1 else ""
            opts_exp = _expand_rpm_macros(opts, macros)
            pkg_name = _extract_pkg_name_from_package_options(opts_exp, source_name)
            if pkg_name:
                _add_capability_tokens(provided, pkg_name, macros)
            continue
        if us.startswith("VERSION:"):
            version = _expand_rpm_macros(s.split(":", 1)[1].strip(), macros)
            continue
        if us.startswith("RELEASE:"):
            release = _expand_rpm_macros(s.split(":", 1)[1].strip(), macros)
            continue
        if us.startswith("EPOCH:"):
            try:
                epoch = int(_expand_rpm_macros(s.split(":", 1)[1].strip(), macros) or "0")
            except ValueError:
                epoch = 0
            continue
        if us.startswith("PROVIDES:"):
            val = s.split(":", 1)[1]
            for tok in _extract_br_tokens_from_tag_value(_expand_rpm_macros(val, macros)):
                if not tok or "%" in tok or tok.startswith("bundled("):
                    continue
                provided.add(tok)
            continue
    defines = {k: str(v) for k, v in bcond.defaults.items()}
    for tok in sorted(active_build_brs(bcond, defines)):
        if "%" in tok:
            continue
        if tok not in br_seen:
            br_seen.add(tok)
            br_out.append(tok)
            br_raw.append(tok)
    for tok in sorted(bcond.unconditional_reqs):
        if "%" in tok:
            continue
        if tok not in req_seen:
            req_seen.add(tok)
            req_out.append(tok)
            req_raw.append(tok)
    provided.add(source_name)
    in_files = False
    for line in (spec_text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        keep = _files_section_continues(s, in_files=in_files)
        if keep is not None:
            in_files = keep
            continue
        if not in_files:
            continue
        expanded = _expand_rpm_macros(s, macros)
        if ".pc" in expanded:
            m = _RE_PC_FILE.search(expanded)
            if m and "%" not in m.group(1) and "*" not in m.group(1):
                provided.add(f"pkgconfig({m.group(1)})")
        if ".so" in expanded:
            m = _RE_SO_FILE.search(expanded)
            if m and "%" not in m.group(1):
                provided.update(_so_file_provides(m.group(1)))
    macros["name"] = source_name
    return _SpecParseResult(
        source_name=source_name,
        build_requires=br_out,
        provided_binary_names=provided,
        build_requires_raw=br_raw,
        requires=req_out,
        requires_raw=req_raw,
        epoch=epoch,
        version=version,
        release=release,
    )


def _provider_map_from_specs(parsed_by_source: dict[str, _SpecParseResult]) -> dict[str, set[str]]:
    provider_map: dict[str, set[str]] = {}
    for src, p in parsed_by_source.items():
        provider_map.setdefault(src, set()).add(src)
        for n in p.provided_binary_names:
            provider_map.setdefault(n, set()).add(src)
    return index_provider_map(provider_map)


def _kahn_layers(
    names: set[str], deps: dict[str, set[str]], rdeps: dict[str, set[str]]
) -> tuple[list[list[str]], bool]:
    remaining = set(names)
    in_deg = {n: len(deps.get(n, set())) for n in names}
    layers: list[list[str]] = []
    cycle = False
    while remaining:
        ready = sorted(n for n in remaining if in_deg.get(n, 0) == 0)
        if not ready:
            cycle = True
            ready = [sorted(remaining)[0]]
        layers.append(ready)
        for n in ready:
            remaining.discard(n)
            for d in rdeps.get(n, set()):
                if d in remaining:
                    in_deg[d] = max(0, in_deg.get(d, 0) - 1)
    return layers, cycle


async def _download_distgit_tarball(
    *,
    owner: str,
    repo: str,
    distgit_branch: str,
    headers: dict[str, str],
) -> tuple[bytes | None, str | None]:
    u = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{distgit_branch}"
    async with httpx.AsyncClient(timeout=180, headers=headers, follow_redirects=True) as client:
        r = await client.get(u)
    if r.status_code != 200:
        return None, f"Tarball fetch failed HTTP {r.status_code}: {u}"
    return r.content, None


_TARBALL_CACHE_TTL_SEC = 300.0
_tarball_cache: dict[tuple[str, str, str, str], tuple[float, bytes]] = {}


async def _resolve_branch_tree_sha(
    *,
    owner: str,
    repo: str,
    distgit_branch: str,
    headers: dict[str, str],
    client: httpx.AsyncClient | None = None,
) -> str:
    if client is not None:
        sha = await _git_tree_sha(
            client,
            owner=owner,
            repo=repo,
            distgit_branch=distgit_branch,
            headers=headers,
        )
    else:
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(
            timeout=timeout, headers=headers, follow_redirects=True
        ) as owned:
            sha = await _git_tree_sha(
                owned,
                owner=owner,
                repo=repo,
                distgit_branch=distgit_branch,
                headers=headers,
            )
    return (sha or "").strip() or "unknown"


async def _get_distgit_tarball_cached(
    *,
    owner: str,
    repo: str,
    distgit_branch: str,
    headers: dict[str, str],
    tree_sha: str | None = None,
) -> tuple[bytes | None, str | None]:
    sha = (tree_sha or "").strip() or await _resolve_branch_tree_sha(
        owner=owner,
        repo=repo,
        distgit_branch=distgit_branch,
        headers=headers,
    )
    key = (owner, repo, distgit_branch, sha)
    now = time.monotonic()
    hit = _tarball_cache.get(key)
    if hit and now - hit[0] < _TARBALL_CACHE_TTL_SEC:
        return hit[1], None
    tarball, err = await _download_distgit_tarball(
        owner=owner,
        repo=repo,
        distgit_branch=distgit_branch,
        headers=headers,
    )
    if tarball:
        _tarball_cache[key] = (now, tarball)
    return tarball, err


def clear_distgit_tarball_cache() -> None:
    _tarball_cache.clear()


def _perl_provide_from_package_name(pkg: str) -> str | None:
    """Fedora-style perl-Foo-Bar packages auto-Provide perl(Foo::Bar)."""
    n = (pkg or "").strip()
    if not n.startswith("perl-"):
        return None
    rest = n[5:]
    if not rest or rest[0].isdigit():
        return None
    low = rest.lower()
    if low in {"interpreter", "libs", "macros", "devel", "generators", "doc", "homedir"}:
        return None
    if low.endswith(("-tests", "-doc", "-debuginfo", "-debugsource")):
        return None
    return f"perl({rest.replace('-', '::')})"


@dataclass
class DistgitProvidesIndex:
    package_names: frozenset[str]
    capability_providers: dict[str, frozenset[str]]
    _indexed: dict[str, set[str]] | None = field(default=None, repr=False, compare=False)

    def indexed_providers(self) -> dict[str, set[str]]:
        if self._indexed is None:
            raw = {k: set(v) for k, v in self.capability_providers.items()}
            for pkg in self.package_names:
                perl_cap = _perl_provide_from_package_name(pkg)
                if perl_cap:
                    raw.setdefault(perl_cap, set()).add(pkg)
            self._indexed = index_provider_map(raw)
        return self._indexed


_PROVIDES_INDEX_CACHE: dict[tuple[str, str, str, str, str], tuple[float, DistgitProvidesIndex]] = {}
_provides_index_tasks: dict[tuple[str, str, str, str, str], asyncio.Task[DistgitProvidesIndex | None]] = {}
_PACKAGE_NAMES_CACHE: dict[tuple[str, str, str, str], tuple[float, frozenset[str]]] = {}
_PACKAGE_NAMES_CACHE_TTL_SEC = 1800.0
_PROVIDES_INDEX_DISK_VERSION = "v3"


def _so_file_provides(libstem: str) -> set[str]:
    stem = (libstem or "").strip()
    if not stem:
        return set()
    if stem.endswith(".so"):
        return {stem}
    return {f"{stem}.so"}


def _provides_match_capability(capability: str, provided: set[str]) -> bool:
    cl = (capability or "").strip().lower()
    if not cl:
        return False
    for p in provided:
        if p.lower() == cl:
            return True
    if cl.startswith("pkgconfig(") and cl.endswith(")"):
        for p in provided:
            if _pkgconfig_glob_match(capability, p):
                return True
            # also allow exact reverse when provide itself is globless
            pl = p.lower()
            if pl.startswith("pkgconfig(") and pl.endswith(")") and pl == cl:
                return True
        return False
    base = cl.split("(", 1)[0]
    if ".so" not in base:
        return False
    for p in provided:
        pl = p.lower().split("(", 1)[0]
        if not pl or ".so" not in pl:
            continue
        if base == pl or base.startswith(pl + ".") or pl.startswith(base + "."):
            return True
    return False


def _lightweight_provides_from_spec_text(pkg: str, spec_text: str) -> set[str]:
    provided: set[str] = {pkg}
    source_name = pkg
    macros = dict(default_distro_macros(""))
    macros.update(_parse_spec_macro_table(spec_text))
    macros["name"] = pkg
    in_files = False
    for line in (spec_text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        us = s.upper()
        low = s.lower()
        if us.startswith("NAME:"):
            source_name = _expand_rpm_macros((s.split(":", 1)[1] or "").strip(), macros) or pkg
            if "%" not in source_name:
                macros["name"] = source_name
                provided.add(source_name)
            in_files = False
            continue
        if us.startswith("PROVIDES:"):
            val = _expand_rpm_macros(s.split(":", 1)[1], macros)
            for tok in _extract_br_tokens_from_tag_value(val):
                if tok and "%" not in tok and not tok.startswith("bundled("):
                    provided.add(tok)
            continue
        if low.startswith("%package"):
            in_files = False
            opts = s.split(None, 1)[1].strip() if len(s.split(None, 1)) > 1 else ""
            opts_exp = _expand_rpm_macros(opts, macros)
            pkg_name = _extract_pkg_name_from_package_options(opts_exp, source_name)
            if pkg_name and "%" not in pkg_name:
                provided.add(pkg_name)
            continue
        keep = _files_section_continues(s, in_files=in_files)
        if keep is not None:
            in_files = keep
            continue
        if not in_files:
            continue
        expanded = _expand_rpm_macros(s, macros)
        if ".pc" in expanded:
            m = _RE_PC_FILE.search(expanded)
            if m and "%" not in m.group(1) and "*" not in m.group(1):
                provided.add(f"pkgconfig({m.group(1)})")
        if ".so" in expanded:
            m = _RE_SO_FILE.search(expanded)
            if m and "%" not in m.group(1):
                provided.update(_so_file_provides(m.group(1)))
    provided.add(source_name)
    for name in (pkg, source_name):
        perl_cap = _perl_provide_from_package_name(name)
        if perl_cap:
            provided.add(perl_cap)
    return provided


def _build_provides_index_from_tarball_sync(tarball: bytes, releasename: str) -> DistgitProvidesIndex:
    """One sequential gzip pass. Dont seek extractfile per package (that re-decompresses forever)."""
    provider_map: dict[str, set[str]] = {}
    names: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r|gz") as tf:
        for m in tf:
            if not m.isfile():
                continue
            parts = m.name.split("/")
            if len(parts) < 4 or parts[1] != releasename:
                continue
            pkg, fn = parts[2], parts[3]
            if fn != f"{pkg}.spec" or not is_distgit_source_name(pkg):
                continue
            f = tf.extractfile(m)
            if f is None:
                continue
            text = f.read(262144).decode("utf-8", errors="replace")
            names.add(pkg)
            for cap in _lightweight_provides_from_spec_text(pkg, text):
                provider_map.setdefault(cap, set()).add(pkg)
    return DistgitProvidesIndex(
        package_names=frozenset(names),
        capability_providers={k: frozenset(v) for k, v in provider_map.items()},
    )


def _provides_index_disk_path(
    owner: str, repo: str, distgit_branch: str, releasename: str, tree_sha: str
) -> Path:
    digest = hashlib.sha256(
        f"{_PROVIDES_INDEX_DISK_VERSION}/{owner}/{repo}/{distgit_branch}/{releasename}/{tree_sha}".encode()
    ).hexdigest()[:24]
    root = Path(os.getenv("OREON_PROVIDES_INDEX_DIR") or (Path.home() / ".oreon" / "provides-index"))
    return root / f"{digest}.json"


def _load_provides_index_disk(path: Path) -> DistgitProvidesIndex | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    pkgs = raw.get("package_names") or []
    caps = raw.get("capability_providers") or {}
    if not isinstance(pkgs, list) or not isinstance(caps, dict):
        return None
    return DistgitProvidesIndex(
        package_names=frozenset(str(p) for p in pkgs),
        capability_providers={
            str(k): frozenset(str(s) for s in (v or []))
            for k, v in caps.items()
            if isinstance(v, list)
        },
    )


def _store_provides_index_disk(path: Path, index: DistgitProvidesIndex) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "package_names": sorted(index.package_names),
        "capability_providers": {
            k: sorted(v) for k, v in sorted(index.capability_providers.items())
        },
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


async def _git_tree_sha(
    client: httpx.AsyncClient,
    *,
    owner: str,
    repo: str,
    distgit_branch: str,
    headers: dict[str, str],
) -> str | None:
    try:
        r = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{distgit_branch}",
            headers=headers,
        )
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    data = r.json() or {}
    sha = ((data.get("commit") or {}).get("tree") or {}).get("sha") or data.get("sha")
    return str(sha) if sha else None


def _build_provides_index_from_specs_sync(
    specs_by_pkg: dict[str, str],
) -> DistgitProvidesIndex:
    provider_map: dict[str, set[str]] = {}
    for pkg, text in specs_by_pkg.items():
        for cap in _lightweight_provides_from_spec_text(pkg, text):
            provider_map.setdefault(cap, set()).add(pkg)
    return DistgitProvidesIndex(
        package_names=frozenset(specs_by_pkg),
        capability_providers={k: frozenset(v) for k, v in provider_map.items()},
    )


async def get_distgit_provides_index_cached(
    *,
    owner: str,
    repo: str,
    distgit_branch: str,
    releasename: str,
    headers: dict[str, str],
) -> DistgitProvidesIndex | None:
    """
    Capability -> distgit source from actual specs at the branch tree SHA.
    Memory + disk keyed by tree SHA so imports on the branch invalidate cleanly.
    """
    timeout = httpx.Timeout(120.0, connect=15.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
    ) as client:
        tree_sha = await _resolve_branch_tree_sha(
            owner=owner,
            repo=repo,
            distgit_branch=distgit_branch,
            headers=headers,
            client=client,
        )

    key = (owner, repo, distgit_branch, releasename, tree_sha)
    now = time.monotonic()
    hit = _PROVIDES_INDEX_CACHE.get(key)
    if hit and now - hit[0] < _TARBALL_CACHE_TTL_SEC:
        return hit[1]

    inflight = _provides_index_tasks.get(key)
    if inflight is not None:
        return await inflight

    async def _load() -> DistgitProvidesIndex | None:
        t0 = time.monotonic()
        disk = _provides_index_disk_path(owner, repo, distgit_branch, releasename, tree_sha)
        if disk.is_file():
            loaded = await asyncio.to_thread(_load_provides_index_disk, disk)
            if loaded is not None:
                _PROVIDES_INDEX_CACHE[key] = (time.monotonic(), loaded)
                logger.info(
                    "distgit provides index %s/%s: disk packages=%s caps=%s tree=%s in %.1fs",
                    releasename,
                    distgit_branch,
                    len(loaded.package_names),
                    len(loaded.capability_providers),
                    tree_sha[:12],
                    time.monotonic() - t0,
                )
                return loaded

        logger.info(
            "distgit provides index %s/%s: building from branch tarball (tree=%s)",
            releasename,
            distgit_branch,
            tree_sha[:12] if tree_sha != "unknown" else tree_sha,
        )
        tarball, err = await _get_distgit_tarball_cached(
            owner=owner,
            repo=repo,
            distgit_branch=distgit_branch,
            headers=headers,
            tree_sha=tree_sha,
        )
        if not tarball:
            if err:
                logger.warning("distgit provides index: %s", err)
            return None
        index = await asyncio.to_thread(
            _build_provides_index_from_tarball_sync, tarball, releasename
        )
        _PROVIDES_INDEX_CACHE[key] = (time.monotonic(), index)
        _clear_github_search_cache_for_branch(owner, repo, distgit_branch, releasename)
        try:
            await asyncio.to_thread(_store_provides_index_disk, disk, index)
        except Exception as exc:
            logger.warning("distgit provides index disk store failed: %s", exc)
        logger.info(
            "distgit provides index %s/%s: packages=%s capabilities=%s tree=%s in %.1fs",
            releasename,
            distgit_branch,
            len(index.package_names),
            len(index.capability_providers),
            tree_sha[:12],
            time.monotonic() - t0,
        )
        return index

    task = asyncio.create_task(_load())
    _provides_index_tasks[key] = task
    try:
        return await task
    finally:
        _provides_index_tasks.pop(key, None)


def clear_distgit_provides_index_cache() -> None:
    _PROVIDES_INDEX_CACHE.clear()
    for task in _provides_index_tasks.values():
        task.cancel()
    _provides_index_tasks.clear()
    _GITHUB_SEARCH_CACHE.clear()


def distgit_providers_dict(index: DistgitProvidesIndex | None) -> dict[str, set[str]]:
    if not index:
        return {}
    return index.indexed_providers()


def _tarball_spec_member_index(
    tarball: bytes, releasename: str
) -> tuple[set[str], dict[str, tarfile.TarInfo]]:
    all_names: set[str] = set()
    members: dict[str, tarfile.TarInfo] = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            parts = m.name.split("/")
            if len(parts) < 4 or parts[1] != releasename:
                continue
            pkg, fn = parts[2], parts[3]
            if fn == f"{pkg}.spec":
                all_names.add(pkg)
                members[pkg] = m
    return all_names, members


def _tarball_spec_package_names(tarball: bytes, releasename: str) -> set[str]:
    all_names, _ = _tarball_spec_member_index(tarball, releasename)
    return all_names


def _read_specs_from_tarball(
    tarball: bytes,
    releasename: str,
    packages: set[str],
    *,
    members: dict[str, tarfile.TarInfo] | None = None,
) -> dict[str, str]:
    want = set(packages)
    if not want:
        return {}
    specs: dict[str, str] = {}
    if members is None:
        _, members = _tarball_spec_member_index(tarball, releasename)
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
        for pkg in want:
            if pkg in specs:
                continue
            m = members.get(pkg)
            if m is None:
                continue
            f = tf.extractfile(m)
            if f is None:
                continue
            specs[pkg] = f.read().decode("utf-8", errors="replace")
    return specs


def _chain_distgit_max_parse() -> int:
    raw = (os.getenv("OREON_CHAIN_DISTGIT_MAX_PARSE") or "4096").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 4096
    return max(64, min(n, 20000))


def _chain_distgit_parse_wave_size() -> int:
    raw = (os.getenv("OREON_CHAIN_DISTGIT_PARSE_WAVE") or "48").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 48
    return max(8, min(n, 128))


def _distgit_spec_raw_url(
    owner: str,
    repo: str,
    distgit_branch: str,
    releasename: str,
    pkg: str,
) -> str:
    path = f"{releasename}/{pkg}/{pkg}.spec"
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{distgit_branch}/{path}"


async def _fetch_distgit_specs_batch(
    client: httpx.AsyncClient,
    *,
    owner: str,
    repo: str,
    distgit_branch: str,
    releasename: str,
    packages: list[str],
) -> dict[str, str]:
    if not packages:
        return {}

    async def one(pkg: str) -> tuple[str, str | None]:
        url = _distgit_spec_raw_url(owner, repo, distgit_branch, releasename, pkg)
        try:
            r = await client.get(url)
        except httpx.HTTPError:
            return pkg, None
        if r.status_code != 200:
            return pkg, None
        return pkg, r.text

    results = await asyncio.gather(*(one(pkg) for pkg in packages))
    return {pkg: text for pkg, text in results if text}


async def expand_chain_spec_closure_sparse(
    roots: list[str],
    *,
    releasename: str,
    owner: str,
    repo: str,
    distgit_branch: str,
    headers: dict[str, str],
    repo_index: Any | None = None,
    target_arches: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, _SpecParseResult], list[str]]:
    err: list[str] = []
    specs: dict[str, str] = {}
    parsed_by_source: dict[str, _SpecParseResult] = {}
    provider_map: dict[str, set[str]] = index_provider_map({})
    known: set[str] = set()
    needed: set[str] = set()
    queue: deque[str] = deque()

    for r in roots:
        rn = (r or "").strip()
        if not rn:
            continue
        needed.add(rn)
        queue.append(rn)

    max_parse = _chain_distgit_max_parse()
    wave_size = _chain_distgit_parse_wave_size()
    pool = _distgit_spec_parse_executor()
    arches = {a.lower() for a in (target_arches or set()) if a} or None
    parse_fn = _spec_parse_fn(releasename, target_arches=arches)
    t0 = time.monotonic()
    fetched = 0

    timeout = httpx.Timeout(30.0, connect=10.0)
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
        limits=limits,
    ) as client:
        while queue and len(needed) <= max_parse:
            wave: list[str] = []
            while queue and len(wave) < wave_size:
                pkg = queue.popleft()
                if pkg in parsed_by_source:
                    continue
                wave.append(pkg)
            if not wave:
                break

            batch_specs = await _fetch_distgit_specs_batch(
                client,
                owner=owner,
                repo=repo,
                distgit_branch=distgit_branch,
                releasename=releasename,
                packages=wave,
            )
            fetched += len(batch_specs)
            for pkg in wave:
                if pkg not in batch_specs:
                    if pkg in roots:
                        err.append(f"chain root {pkg!r} not found under {releasename}/ in distgit")
                    continue
                known.add(pkg)

            futures = {
                pool.submit(parse_fn, batch_specs[pkg], f"{pkg}.spec"): pkg
                for pkg in batch_specs
            }
            for fut in as_completed(futures):
                pkg = futures[fut]
                try:
                    parsed = fut.result()
                except Exception as exc:
                    err.append(f"{pkg}: parse failed ({type(exc).__name__}: {exc})")
                    parsed = _SpecParseResult(
                        source_name=pkg,
                        build_requires=[],
                        provided_binary_names={pkg},
                        build_requires_raw=[],
                    )
                specs[pkg] = batch_specs[pkg]
                parsed_by_source[pkg] = parsed
                for cap, srcs in _provider_map_from_specs({pkg: parsed}).items():
                    if cap == "__lower__":
                        continue
                    add_capability_providers(provider_map, cap, srcs)
                for dep in dependency_capabilities(parsed):
                    if repo_index is not None and repo_index.satisfies_requirement(dep):
                        continue
                    for cap in requirement_check_capabilities(dep):
                        for prov in _providers_from_maps(cap, [provider_map]):
                            if prov in parsed_by_source or prov == pkg:
                                continue
                            if not is_distgit_source_name(prov):
                                continue
                            if prov not in needed:
                                needed.add(prov)
                                queue.append(prov)
                        if repo_index is not None:
                            for src in repo_index.distgit_sources_for_capability(cap):
                                if src in parsed_by_source or src == pkg:
                                    continue
                                if not is_distgit_source_name(src):
                                    continue
                                if src not in needed:
                                    needed.add(src)
                                    queue.append(src)

    if len(needed) > max_parse:
        err.append(
            f"Chain distgit closure exceeded OREON_CHAIN_DISTGIT_MAX_PARSE ({max_parse}); narrow the chain spec."
        )

    logger.info(
        "sparse distgit closure %s/%s: roots=%s fetched=%s parsed=%s in %.1fs",
        releasename,
        distgit_branch,
        len([r for r in roots if (r or "").strip()]),
        fetched,
        len(parsed_by_source),
        time.monotonic() - t0,
    )
    return specs, parsed_by_source, err


async def _parse_specs_parallel_async(
    specs_by_pkg: dict[str, str],
    *,
    releasename: str = "",
) -> tuple[dict[str, _SpecParseResult], list[str]]:
    err: list[str] = []
    parsed_by_source: dict[str, _SpecParseResult] = {}
    if not specs_by_pkg:
        return parsed_by_source, err
    loop = asyncio.get_running_loop()
    pool = _distgit_spec_parse_executor()
    parse_fn = _spec_parse_fn(releasename)
    items = list(specs_by_pkg.items())
    tasks = [
        loop.run_in_executor(pool, parse_fn, spec_text, f"{pkg}.spec")
        for pkg, spec_text in items
    ]
    parse_results = await asyncio.gather(*tasks, return_exceptions=True)
    for (pkg, _spec_text), parsed_or_exc in zip(items, parse_results, strict=False):
        if isinstance(parsed_or_exc, Exception):
            err.append(f"{pkg}: parse failed ({type(parsed_or_exc).__name__}: {parsed_or_exc})")
            parsed_by_source[pkg] = _SpecParseResult(
                source_name=pkg,
                build_requires=[],
                provided_binary_names={pkg},
                build_requires_raw=[],
            )
        else:
            parsed_by_source[pkg] = parsed_or_exc
    return parsed_by_source, err


def _distgit_order_from_parsed(
    *,
    releasename: str,
    distgit_url: str,
    distgit_branch: str,
    parsed_by_source: dict[str, _SpecParseResult],
    err: list[str],
) -> DistgitBuildOrderResult:
    names_set: set[str] = set(parsed_by_source.keys())
    edges: list[dict[str, str]] = []
    deps: dict[str, set[str]] = {n: set() for n in names_set}
    rdeps: dict[str, set[str]] = {n: set() for n in names_set}
    provider_map = _provider_map_from_specs(parsed_by_source)

    for cons, parsed in parsed_by_source.items():
        for dep in parsed.dependency_names():
            providers = provider_map.get(dep) or set()
            for prov in sorted(providers):
                if prov not in names_set or prov == cons:
                    continue
                deps[cons].add(prov)
                rdeps[prov].add(cons)
                edges.append(
                    {
                        "from": cons,
                        "to": prov,
                        "requirement": dep,
                        "satisfied": False,
                        "type": "distgit_br",
                    }
                )

    layers, cyc = _kahn_layers(names_set, deps, rdeps)
    if cyc:
        err.append("Warning: a cycle exists in distgit BuildRequires; some parallel layers may be conservative.")

    capability_providers = {
        cap: sorted(srcs)
        for cap, srcs in sorted(provider_map.items())
        if cap != "__lower__" and isinstance(srcs, set)
    }
    spec_evrs = {src: parsed.evr_canonical() for src, parsed in parsed_by_source.items()}
    build_requires_raw = {src: list(parsed.build_requires_raw) for src, parsed in parsed_by_source.items()}

    return DistgitBuildOrderResult(
        releasename=releasename,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        package_names=sorted(names_set),
        edges=edges,
        layers=layers,
        capability_providers=capability_providers,
        spec_evrs=spec_evrs,
        build_requires_raw=build_requires_raw,
        errors=err,
        cycle_warning=cyc,
    )


def _expand_chain_spec_closure(
    roots: list[str],
    all_names: set[str],
    tarball: bytes,
    releasename: str,
    *,
    members: dict[str, tarfile.TarInfo] | None = None,
) -> tuple[dict[str, str], dict[str, _SpecParseResult], list[str]]:
    err: list[str] = []
    names_set = set(all_names)
    if members is None:
        names_set, members = _tarball_spec_member_index(tarball, releasename)
    specs: dict[str, str] = {}
    parsed_by_source: dict[str, _SpecParseResult] = {}
    provider_map: dict[str, set[str]] = index_provider_map({})
    needed: set[str] = set()
    queue: deque[str] = deque()

    for r in roots:
        rn = (r or "").strip()
        if not rn:
            continue
        if rn not in names_set:
            err.append(f"chain root {rn!r} not found under {releasename}/ in distgit")
            continue
        needed.add(rn)
        queue.append(rn)

    max_parse = _chain_distgit_max_parse()
    wave_size = _chain_distgit_parse_wave_size()
    pool = _distgit_spec_parse_executor()
    parse_fn = _spec_parse_fn(releasename)

    while queue and len(needed) <= max_parse:
        wave: list[str] = []
        while queue and len(wave) < wave_size:
            pkg = queue.popleft()
            if pkg in parsed_by_source:
                continue
            wave.append(pkg)
        if not wave:
            break

        batch_specs = _read_specs_from_tarball(
            tarball, releasename, set(wave), members=members
        )
        for pkg in wave:
            if pkg not in batch_specs:
                err.append(f"spec missing in tarball for {pkg}")

        futures = {
            pool.submit(parse_fn, batch_specs[pkg], f"{pkg}.spec"): pkg
            for pkg in batch_specs
        }
        for fut in as_completed(futures):
            pkg = futures[fut]
            try:
                parsed = fut.result()
            except Exception as exc:
                err.append(f"{pkg}: parse failed ({type(exc).__name__}: {exc})")
                parsed = _SpecParseResult(
                    source_name=pkg,
                    build_requires=[],
                    provided_binary_names={pkg},
                    build_requires_raw=[],
                )
            specs[pkg] = batch_specs[pkg]
            parsed_by_source[pkg] = parsed
            for cap, srcs in _provider_map_from_specs({pkg: parsed}).items():
                if cap == "__lower__":
                    continue
                add_capability_providers(provider_map, cap, srcs)
            for dep in dependency_capabilities(parsed):
                provs = set(_providers_from_maps(dep, [provider_map]))
                if dep in names_set:
                    provs.add(dep)
                for prov in provs:
                    if prov not in names_set or prov == pkg:
                        continue
                    if prov not in needed:
                        needed.add(prov)
                        queue.append(prov)

    if len(needed) > max_parse:
        err.append(
            f"Chain distgit closure exceeded OREON_CHAIN_DISTGIT_MAX_PARSE ({max_parse}); narrow the chain spec."
        )
    return specs, parsed_by_source, err


async def expand_chain_spec_closure_tarball(
    roots: list[str],
    *,
    releasename: str,
    owner: str,
    repo: str,
    distgit_branch: str,
    headers: dict[str, str],
) -> tuple[dict[str, str], dict[str, _SpecParseResult], list[str]]:
    err: list[str] = []
    t0 = time.monotonic()
    tarball, terr = await _get_distgit_tarball_cached(
        owner=owner,
        repo=repo,
        distgit_branch=distgit_branch,
        headers=headers,
    )
    if terr:
        err.append(terr)
    if not tarball:
        return {}, {}, err or ["distgit tarball fetch returned no data"]

    all_names, members = await asyncio.to_thread(_tarball_spec_member_index, tarball, releasename)
    specs, parsed_by_source, cerr = await asyncio.to_thread(
        _expand_chain_spec_closure,
        roots,
        all_names,
        tarball,
        releasename,
        members=members,
    )
    err.extend(cerr)
    logger.info(
        "tarball distgit closure %s/%s: roots=%s parsed=%s in %.1fs",
        releasename,
        distgit_branch,
        len([r for r in roots if (r or "").strip()]),
        len(parsed_by_source),
        time.monotonic() - t0,
    )
    return specs, parsed_by_source, err


def _chain_repo_transitive_max_waves() -> int:
    raw = (os.getenv("OREON_CHAIN_REPO_TRANSITIVE_MAX_WAVES") or "12").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 12
    return max(1, min(n, 64))


def _repo_caps_to_walk(repo: Any, br: str) -> list[str]:
    checks = requirement_check_capabilities(br)
    if not checks:
        raw = (br or "").strip()
        return [raw] if raw else []
    satisfied = [c for c in checks if repo.has_capability(c)]
    return satisfied if satisfied else checks


def _repo_transitive_miss_sources(
    repo: Any,
    cap: str,
    *,
    distgit_providers: dict[str, set[str]] | None = None,
) -> tuple[set[str], set[str]]:
    missing_caps: set[str] = set()
    provider_sources: set[str] = set()
    maps = [distgit_providers] if distgit_providers else []
    for walk_cap in _repo_caps_to_walk(repo, cap):
        for miss in repo.transitive_unsatisfied(walk_cap):
            missing_caps.add(miss)
            provider_sources.update(repo.distgit_sources_for_capability(miss))
            provider_sources.update(_providers_from_maps(miss, maps))
    return missing_caps, {s for s in provider_sources if s and is_distgit_source_name(s)}


def _collect_repo_transitive_candidates(
    parsed_by_source: dict[str, Any],
    repo: Any,
    *,
    scope_packages: set[str] | None = None,
    distgit_providers: dict[str, set[str]] | None = None,
) -> set[str]:
    candidates: set[str] = set()
    if scope_packages is not None:
        pkg_iter = (
            (pkg, parsed_by_source[pkg])
            for pkg in scope_packages
            if pkg in parsed_by_source
        )
    else:
        pkg_iter = parsed_by_source.items()

    scope_map = _provider_map_from_specs(parsed_by_source)
    provider_maps = [scope_map, distgit_providers or {}]
    seen_brs: set[str] = set()

    for _pkg, parsed in pkg_iter:
        for cap in dependency_capabilities(parsed):
            if cap in seen_brs:
                continue
            seen_brs.add(cap)
            _missing_caps, provider_sources = _repo_transitive_miss_sources(
                repo,
                cap,
                distgit_providers=distgit_providers,
            )
            candidates.update(provider_sources)
            for miss in _missing_caps:
                candidates.update(
                    sources_for_requirement(
                        miss,
                        repo,
                        provider_maps=provider_maps,
                    )
                )
                # also take direct github/distgit provider hits for the missing runtime cap
                for src in _providers_from_maps(miss, provider_maps):
                    if src and is_distgit_source_name(src) and not repo.satisfies_source(src):
                        candidates.add(src)
            if repo.satisfies_requirement(cap):
                continue
            candidates.update(
                sources_for_requirement(
                    cap,
                    repo,
                    provider_maps=provider_maps,
                )
            )

    return {c for c in candidates if c and is_distgit_source_name(c)}


_GITHUB_SEARCH_CACHE: dict[tuple[str, ...], tuple[float, frozenset[str]]] = {}
_GITHUB_SEARCH_CACHE_TTL_SEC = 600.0


def _clear_github_search_cache_for_branch(
    owner: str, repo: str, distgit_branch: str, releasename: str
) -> None:
    branch = (distgit_branch or "main").strip() or "main"
    drop = [
        k
        for k in _GITHUB_SEARCH_CACHE
        if len(k) >= 4
        and k[0] == owner
        and k[1] == repo
        and k[2] == releasename
        and k[3] == branch
    ]
    for k in drop:
        _GITHUB_SEARCH_CACHE.pop(k, None)


def _source_name_from_distgit_spec_path(path: str, releasename: str) -> str | None:
    parts = (path or "").strip().split("/")
    if len(parts) < 3 or parts[0] != releasename:
        return None
    pkg, fn = parts[1], parts[2]
    if fn != f"{pkg}.spec":
        return None
    if not is_distgit_source_name(pkg):
        return None
    return pkg


def _so_search_tokens(capability: str) -> list[str]:
    """Literal soname fragments only. No inventing package names."""
    cap = (capability or "").strip()
    if not cap:
        return []
    base = cap.split("(", 1)[0].strip()
    if ".so" not in base:
        return []
    out: list[str] = [base]
    # libIL.so.1 -> also search libIL.so which is what %files usually writes
    m = re.match(r"^(lib[A-Za-z0-9_+.-]+\.so)(?:\.\d+)*$", base)
    if m and m.group(1) != base:
        out.append(m.group(1))
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        tl = t.lower()
        if tl in seen:
            continue
        seen.add(tl)
        uniq.append(t)
    return uniq


async def _get_distgit_package_names_cached(
    client: httpx.AsyncClient,
    *,
    owner: str,
    repo: str,
    releasename: str,
    distgit_branch: str,
    headers: dict[str, str],
) -> frozenset[str]:
    branch = (distgit_branch or "main").strip() or "main"
    key = (owner, repo, branch, releasename)
    now = time.monotonic()
    hit = _PACKAGE_NAMES_CACHE.get(key)
    if hit and now - hit[0] < _PACKAGE_NAMES_CACHE_TTL_SEC:
        return hit[1]
    try:
        r = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}",
            headers=headers,
            params={"recursive": "1"},
        )
    except httpx.HTTPError:
        return frozenset()
    if r.status_code != 200:
        return frozenset()
    names: set[str] = set()
    prefix = f"{releasename}/"
    for item in r.json().get("tree") or []:
        p = item.get("path") or ""
        if not p.startswith(prefix) or not p.endswith(".spec"):
            continue
        parts = p.split("/")
        if len(parts) != 3:
            continue
        pkg, fn = parts[1], parts[2]
        if fn == f"{pkg}.spec" and is_distgit_source_name(pkg):
            names.add(pkg)
    frozen = frozenset(names)
    _PACKAGE_NAMES_CACHE[key] = (now, frozen)
    return frozen


def _warm_provides_index_lookup(capability: str) -> set[str]:
    cap = (capability or "").strip()
    if not cap:
        return set()
    out: set[str] = set()
    now = time.monotonic()
    for _key, (ts, index) in list(_PROVIDES_INDEX_CACHE.items()):
        if now - ts >= _TARBALL_CACHE_TTL_SEC:
            continue
        out.update(_providers_from_maps(cap, [distgit_providers_dict(index)]))
    return {s for s in out if s and is_distgit_source_name(s)}


async def _verify_spec_provides_capability(
    client: httpx.AsyncClient,
    *,
    owner: str,
    repo: str,
    releasename: str,
    distgit_branch: str,
    headers: dict[str, str],
    source: str,
    capability: str,
) -> bool:
    branch = (distgit_branch or "main").strip() or "main"
    url = (
        f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/"
        f"{releasename}/{source}/{source}.spec"
    )
    try:
        r = await client.get(url, headers=headers)
    except httpx.HTTPError:
        return False
    if r.status_code != 200:
        return False
    provided = _lightweight_provides_from_spec_text(source, r.text)
    return _provides_match_capability(capability, provided)


def _search_queries_for_capability(owner: str, repo: str, releasename: str, capability: str) -> list[str]:
    cap = (capability or "").strip()
    if not cap:
        return []
    queries = [f'repo:{owner}/{repo} path:{releasename}/ filename:.spec "Provides: {cap}"']
    if cap.startswith("pkgconfig(") and cap.endswith(")"):
        inner = cap[len("pkgconfig(") : -1].strip()
        if inner and "*" not in inner:
            queries.append(
                f'repo:{owner}/{repo} path:{releasename}/ filename:.spec "{inner}.pc"'
            )
    for tok in _so_search_tokens(cap):
        if ".so" in tok:
            queries.append(f'repo:{owner}/{repo} path:{releasename}/ filename:.spec "{tok}"')
    if is_distgit_source_name(cap):
        queries.append(
            f'repo:{owner}/{repo} path:{releasename}/ filename:.spec "%package -n {cap}"'
        )
        queries.append(
            f'repo:{owner}/{repo} path:{releasename}/ filename:{cap}.spec'
        )
    # de-dupe
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        if q in seen:
            continue
        seen.add(q)
        out.append(q)
    return out


async def _github_search_distgit_providers(
    client: httpx.AsyncClient,
    *,
    owner: str,
    repo: str,
    releasename: str,
    capability: str,
    headers: dict[str, str],
    distgit_branch: str = "main",
    allow_network: bool = False,
) -> set[str]:
    """
    Resolve capability -> distgit source.
    When the Provides index for this branch is warm, that index is authoritative.
    Network search is only for cold-index fallback.
    """
    cap = (capability or "").strip()
    if not cap:
        return set()
    branch = (distgit_branch or "main").strip() or "main"
    cache_key = (owner, repo, releasename, branch, cap.lower())
    now = time.monotonic()
    hit = _GITHUB_SEARCH_CACHE.get(cache_key)
    if hit and now - hit[0] < _GITHUB_SEARCH_CACHE_TTL_SEC:
        return set(hit[1])

    sources: set[str] = set()
    sources.update(_warm_provides_index_lookup(cap))
    index_warm = _provides_index_is_warm(owner, repo, branch, releasename)

    # Index for this tree SHA is the source of truth. No code-search spam on misses.
    if sources or (index_warm and not allow_network):
        _GITHUB_SEARCH_CACHE[cache_key] = (now, frozenset(sources))
        return sources

    for q in _search_queries_for_capability(owner, repo, releasename, cap)[:5]:
        try:
            r = await client.get(
                "https://api.github.com/search/code",
                params={"q": q, "per_page": 15},
                headers=headers,
            )
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        for item in r.json().get("items") or []:
            src = _source_name_from_distgit_spec_path(item.get("path") or "", releasename)
            if not src:
                continue
            if await _verify_spec_provides_capability(
                client,
                owner=owner,
                repo=repo,
                releasename=releasename,
                distgit_branch=branch,
                headers=headers,
                source=src,
                capability=cap,
            ):
                sources.add(src)
        if sources:
            break

    if not sources and is_distgit_source_name(cap):
        try:
            r = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{releasename}/{cap}/{cap}.spec",
                headers=headers,
                params={"ref": branch},
            )
        except httpx.HTTPError:
            r = None
        if r is not None and r.status_code == 200:
            if await _verify_spec_provides_capability(
                client,
                owner=owner,
                repo=repo,
                releasename=releasename,
                distgit_branch=branch,
                headers=headers,
                source=cap,
                capability=cap,
            ):
                sources.add(cap)

    _GITHUB_SEARCH_CACHE[cache_key] = (now, frozenset(sources))
    return sources


def _provides_index_is_warm(
    owner: str, repo: str, distgit_branch: str, releasename: str
) -> bool:
    now = time.monotonic()
    for key, (ts, _index) in _PROVIDES_INDEX_CACHE.items():
        if len(key) < 4:
            continue
        o, r, b, rel = key[0], key[1], key[2], key[3]
        if o == owner and r == repo and b == distgit_branch and rel == releasename:
            if now - ts < _TARBALL_CACHE_TTL_SEC:
                return True
    return False




def _missing_runtime_caps_for_scope(
    parsed_by_source: dict[str, Any],
    repo: Any,
    *,
    scope_packages: set[str] | None = None,
) -> set[str]:
    out: set[str] = set()
    if scope_packages is not None:
        pkg_iter = (
            (pkg, parsed_by_source[pkg])
            for pkg in scope_packages
            if pkg in parsed_by_source
        )
    else:
        pkg_iter = parsed_by_source.items()
    seen: set[str] = set()
    for _pkg, parsed in pkg_iter:
        for cap in dependency_capabilities(parsed):
            if cap in seen:
                continue
            seen.add(cap)
            for walk_cap in _repo_caps_to_walk(repo, cap):
                out.update(repo.missing_runtime_caps_for_requirement(walk_cap))
    return out


async def _resolve_missing_runtime_via_github(
    parsed_by_source: dict[str, _SpecParseResult],
    repo: Any,
    providers: dict[str, set[str]],
    *,
    owner: str,
    repo_name: str,
    releasename: str,
    headers: dict[str, str],
    scope_packages: set[str] | None,
    client: httpx.AsyncClient,
    distgit_branch: str = "main",
) -> int:
    caps = _missing_runtime_caps_for_scope(
        parsed_by_source,
        repo,
        scope_packages=scope_packages,
    )
    resolved = 0
    for cap in sorted(caps)[:24]:
        if _providers_from_maps(cap, [providers]):
            continue
        if repo.distgit_sources_for_capability(cap):
            continue
        sources = await _github_search_distgit_providers(
            client,
            owner=owner,
            repo=repo_name,
            releasename=releasename,
            capability=cap,
            headers=headers,
            distgit_branch=distgit_branch,
        )
        if not sources:
            continue
        add_capability_providers(providers, cap, sources)
        resolved += 1
    return resolved


def _unresolved_capabilities(
    specs_by_pkg: dict[str, str],
    parsed_by_source: dict[str, _SpecParseResult],
    repo: Any,
    *,
    distgit_providers: dict[str, set[str]] | None = None,
    scope_packages: set[str] | None = None,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
) -> set[str]:
    out: set[str] = set()
    chain_providers = _provider_map_from_specs(parsed_by_source)
    pkg_iter = (
        sorted(scope_packages)
        if scope_packages is not None
        else sorted(parsed_by_source.keys())
    )
    for pkg in pkg_iter:
        if pkg not in parsed_by_source:
            continue
        spec_text = specs_by_pkg.get(pkg) or ""
        if not spec_text:
            continue
        bcond = parse_spec_bconds(
            spec_text, distro_macros=distro_macros, target_arches=target_arches
        )
        defines = default_build_defines(bcond)
        for cap in iter_plan_capabilities(parsed_by_source[pkg], bcond, repo):
            if "%" in cap or repo.satisfies_requirement(cap):
                continue
            if _distgit_fetch_candidates_for_br(
                cap,
                repo,
                distgit_providers=distgit_providers,
                chain_providers=chain_providers,
            ):
                continue
            primary = rpm_requirement_primary(cap)
            if primary:
                out.add(primary)
    return out


async def _resolve_capabilities_via_github(
    specs_by_pkg: dict[str, str],
    parsed_by_source: dict[str, _SpecParseResult],
    repo: Any,
    providers: dict[str, set[str]],
    *,
    owner: str,
    repo_name: str,
    releasename: str,
    headers: dict[str, str],
    scope_packages: set[str] | None,
    client: httpx.AsyncClient,
    distgit_branch: str = "main",
    target_arches: set[str] | None = None,
) -> int:
    caps = _unresolved_capabilities(
        specs_by_pkg,
        parsed_by_source,
        repo,
        distgit_providers=providers,
        scope_packages=scope_packages,
        distro_macros=default_distro_macros(releasename),
        target_arches=target_arches,
    )
    resolved = 0
    for cap in sorted(caps)[:48]:
        if _providers_from_maps(cap, [providers]):
            continue
        sources = await _github_search_distgit_providers(
            client,
            owner=owner,
            repo=repo_name,
            releasename=releasename,
            capability=cap,
            headers=headers,
            distgit_branch=distgit_branch,
        )
        if not sources:
            continue
        add_capability_providers(providers, cap, sources)
        resolved += 1
    return resolved


def _distgit_fetch_candidates_for_br(
    br: str,
    repo: Any,
    *,
    distgit_providers: dict[str, set[str]] | None = None,
    chain_providers: dict[str, set[str]] | None = None,
) -> set[str]:
    maps: list[dict[str, set[str]]] = []
    if chain_providers:
        maps.append(chain_providers)
    if distgit_providers:
        maps.append(distgit_providers)
    return sources_for_requirement(br, repo, provider_maps=maps)


def _distgit_source_needers(
    specs_by_pkg: dict[str, str],
    parsed_by_source: dict[str, _SpecParseResult],
    repo: Any,
    *,
    distgit_providers: dict[str, set[str]] | None = None,
    scope_packages: set[str] | None = None,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
) -> dict[str, set[str]]:
    needers: dict[str, set[str]] = {}
    chain_providers = _provider_map_from_specs(parsed_by_source)
    pkg_iter = (
        sorted(scope_packages)
        if scope_packages is not None
        else sorted(parsed_by_source.keys())
    )
    for pkg in pkg_iter:
        if pkg not in parsed_by_source:
            continue
        spec_text = specs_by_pkg.get(pkg) or ""
        if not spec_text:
            continue
        bcond = parse_spec_bconds(
            spec_text, distro_macros=distro_macros, target_arches=target_arches
        )
        for cap in iter_plan_capabilities(parsed_by_source[pkg], bcond, repo):
            if "%" in cap:
                continue
            for src in _distgit_fetch_candidates_for_br(
                cap,
                repo,
                distgit_providers=distgit_providers,
                chain_providers=chain_providers,
            ):
                needers.setdefault(src, set()).add(pkg)
    return needers


def _collect_unsatisfied_build_dep_sources(
    specs_by_pkg: dict[str, str],
    parsed_by_source: dict[str, _SpecParseResult],
    repo: Any,
    *,
    distgit_providers: dict[str, set[str]] | None = None,
    scope_packages: set[str] | None = None,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
) -> set[str]:
    out: set[str] = set()
    chain_providers = _provider_map_from_specs(parsed_by_source)
    pkg_iter = (
        sorted(scope_packages)
        if scope_packages is not None
        else sorted(parsed_by_source.keys())
    )
    for pkg in pkg_iter:
        if pkg not in parsed_by_source:
            continue
        spec_text = specs_by_pkg.get(pkg) or ""
        if not spec_text:
            continue
        bcond = parse_spec_bconds(
            spec_text, distro_macros=distro_macros, target_arches=target_arches
        )
        for cap in iter_plan_capabilities(parsed_by_source[pkg], bcond, repo):
            if "%" in cap:
                continue
            out.update(
                _distgit_fetch_candidates_for_br(
                    cap,
                    repo,
                    distgit_providers=distgit_providers,
                    chain_providers=chain_providers,
                )
            )
    return out


def _has_unresolved_distgit_build_deps(
    specs_by_pkg: dict[str, str],
    parsed_by_source: dict[str, _SpecParseResult],
    repo: Any,
    *,
    distgit_providers: dict[str, set[str]] | None = None,
    scope_packages: set[str] | None = None,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
) -> bool:
    return bool(
        _unresolved_capabilities(
            specs_by_pkg,
            parsed_by_source,
            repo,
            distgit_providers=distgit_providers,
            scope_packages=scope_packages,
            distro_macros=distro_macros,
            target_arches=target_arches,
        )
    )


async def expand_unsatisfied_build_dep_sources(
    specs: dict[str, str],
    parsed_by_source: dict[str, _SpecParseResult],
    repo: Any,
    *,
    releasename: str,
    owner: str,
    repo_name: str,
    distgit_branch: str,
    headers: dict[str, str],
    distgit_providers: dict[str, set[str]] | None = None,
    scope_packages: set[str] | None = None,
    target_arches: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, _SpecParseResult], list[str], set[str]]:
    err: list[str] = []
    missing_distgit: set[str] = set()
    if not parsed_by_source:
        return specs, parsed_by_source, err, missing_distgit

    providers = distgit_providers if distgit_providers is not None else {}
    arches = {a.lower() for a in (target_arches or set()) if a} or None
    active_scope = set(scope_packages) if scope_packages is not None else None

    max_parse = _chain_distgit_max_parse()
    wave_size = _chain_distgit_parse_wave_size()
    pool = _distgit_spec_parse_executor()
    parse_fn = _spec_parse_fn(releasename, target_arches=arches)
    distro_macros = default_distro_macros(releasename)
    t0 = time.monotonic()
    fetched_total = 0
    attempted_fetch: set[str] = set()
    timeout = httpx.Timeout(30.0, connect=10.0)
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
        limits=limits,
    ) as client:
        for wave_num in range(1, 17):
            if _has_unresolved_distgit_build_deps(
                specs,
                parsed_by_source,
                repo,
                distgit_providers=providers,
                scope_packages=active_scope,
                distro_macros=distro_macros,
                target_arches=arches,
            ):
                n = await _resolve_capabilities_via_github(
                    specs,
                    parsed_by_source,
                    repo,
                    providers,
                    owner=owner,
                    repo_name=repo_name,
                    releasename=releasename,
                    headers=headers,
                    scope_packages=active_scope,
                    client=client,
                    distgit_branch=distgit_branch,
                    target_arches=arches,
                )
                if n:
                    logger.info(
                        "github provides lookup %s/%s: resolved=%s cached_caps=%s",
                        releasename,
                        distgit_branch,
                        n,
                        len(providers),
                    )

            candidates = await asyncio.to_thread(
                _collect_unsatisfied_build_dep_sources,
                specs,
                parsed_by_source,
                repo,
                distgit_providers=providers,
                scope_packages=active_scope,
                distro_macros=distro_macros,
                target_arches=arches,
            )
            needers = await asyncio.to_thread(
                _distgit_source_needers,
                specs,
                parsed_by_source,
                repo,
                distgit_providers=providers,
                scope_packages=active_scope,
                distro_macros=distro_macros,
                target_arches=arches,
            )
            to_fetch = sorted(
                c for c in candidates if c not in parsed_by_source and c not in attempted_fetch
            )
            if not to_fetch or len(parsed_by_source) >= max_parse:
                break
            batch_specs = await _fetch_distgit_specs_batch(
                client,
                owner=owner,
                repo=repo_name,
                distgit_branch=distgit_branch,
                releasename=releasename,
                packages=to_fetch[:wave_size],
            )
            for pkg in to_fetch[:wave_size]:
                if pkg not in batch_specs:
                    attempted_fetch.add(pkg)
                    missing_distgit.add(pkg)
                    by = sorted(needers.get(pkg, []))
                    who = ", ".join(by[:6]) if by else "unsatisfied build dep"
                    err.append(
                        f"{pkg}: not in distgit at {releasename}/{pkg}/ (needed by {who})"
                    )
            if not batch_specs:
                break
            fetched_total += len(batch_specs)
            futures = {
                pool.submit(parse_fn, batch_specs[pkg], f"{pkg}.spec"): pkg
                for pkg in batch_specs
            }
            for fut in as_completed(futures):
                pkg = futures[fut]
                try:
                    parsed = fut.result()
                except Exception as exc:
                    err.append(f"{pkg}: parse failed ({type(exc).__name__}: {exc})")
                    parsed = _SpecParseResult(
                        source_name=pkg,
                        build_requires=[],
                        provided_binary_names={pkg},
                        build_requires_raw=[],
                    )
                specs[pkg] = batch_specs[pkg]
                parsed_by_source[pkg] = parsed
            # newly fetched packages must be walked for their own BRs
            if active_scope is not None:
                active_scope.update(batch_specs.keys())

    logger.info(
        "unsatisfied builddep distgit %s/%s: fetched=%s parsed=%s in %.1fs",
        releasename,
        distgit_branch,
        fetched_total,
        len(parsed_by_source),
        time.monotonic() - t0,
    )
    return specs, parsed_by_source, err, missing_distgit


async def fetch_repo_transitive_extras_for_roots(
    specs: dict[str, str],
    parsed_by_source: dict[str, _SpecParseResult],
    repo: Any,
    roots: list[str],
    *,
    releasename: str,
    owner: str,
    repo_name: str,
    distgit_branch: str,
    headers: dict[str, str],
    distgit_providers: dict[str, set[str]] | None = None,
    target_arches: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, _SpecParseResult], list[str]]:
    err: list[str] = []
    root_set = {(r or "").strip() for r in roots if (r or "").strip()}
    if not root_set or not parsed_by_source:
        return specs, parsed_by_source, err

    arches = {a.lower() for a in (target_arches or set()) if a} or None
    parse_fn = _spec_parse_fn(releasename, target_arches=arches)
    t0 = time.monotonic()
    timeout = httpx.Timeout(30.0, connect=10.0)
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
    fetched_total = 0
    providers = distgit_providers if distgit_providers is not None else {}
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
        limits=limits,
    ) as client:
        for _ in range(3):
            n = await _resolve_missing_runtime_via_github(
                parsed_by_source,
                repo,
                providers,
                owner=owner,
                repo_name=repo_name,
                releasename=releasename,
                headers=headers,
                scope_packages=root_set,
                client=client,
                distgit_branch=distgit_branch,
            )
            if n:
                logger.info(
                    "runtime provides lookup %s/%s: resolved=%s cached_caps=%s",
                    releasename,
                    distgit_branch,
                    n,
                    len(providers),
                )
            candidates = await asyncio.to_thread(
                _collect_repo_transitive_candidates,
                parsed_by_source,
                repo,
                scope_packages=root_set,
                distgit_providers=providers,
            )
            to_fetch = sorted(c for c in candidates if c not in parsed_by_source)
            if not to_fetch:
                break
            batch_specs = await _fetch_distgit_specs_batch(
                client,
                owner=owner,
                repo=repo_name,
                distgit_branch=distgit_branch,
                releasename=releasename,
                packages=to_fetch[:16],
            )
            if not batch_specs:
                break
            fetched_total += len(batch_specs)
            for pkg, text in batch_specs.items():
                try:
                    parsed = await asyncio.to_thread(parse_fn, text, f"{pkg}.spec")
                except Exception as exc:
                    err.append(f"{pkg}: parse failed ({type(exc).__name__}: {exc})")
                    parsed = _SpecParseResult(
                        source_name=pkg,
                        build_requires=[],
                        provided_binary_names={pkg},
                        build_requires_raw=[],
                    )
                specs[pkg] = text
                parsed_by_source[pkg] = parsed
                for cap in list(parsed.provided_binary_names) + [pkg]:
                    add_capability_providers(providers, cap, {pkg})

    logger.info(
        "repo transitive extras %s/%s: fetched=%s in %.1fs",
        releasename,
        distgit_branch,
        fetched_total,
        time.monotonic() - t0,
    )
    return specs, parsed_by_source, err


async def expand_closure_for_repo_transitive_deps(
    specs: dict[str, str],
    parsed_by_source: dict[str, _SpecParseResult],
    repo: Any,
    *,
    releasename: str,
    owner: str,
    repo_name: str,
    distgit_branch: str,
    headers: dict[str, str],
    scope_packages: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, _SpecParseResult], list[str]]:
    err: list[str] = []
    if not parsed_by_source:
        return specs, parsed_by_source, err

    max_parse = _chain_distgit_max_parse()
    wave_size = _chain_distgit_parse_wave_size()
    pool = _distgit_spec_parse_executor()
    parse_fn = _spec_parse_fn(releasename)
    t0 = time.monotonic()
    fetched_extra = 0
    logger.info(
        "repo transitive distgit closure %s/%s: start parsed=%s",
        releasename,
        distgit_branch,
        len(parsed_by_source),
    )

    timeout = httpx.Timeout(30.0, connect=10.0)
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
        limits=limits,
    ) as client:
        max_waves = _chain_repo_transitive_max_waves()
        wave_num = 0
        to_fetch: list[str] = []
        attempted_fetch: set[str] = set()
        while len(parsed_by_source) <= max_parse and wave_num < max_waves:
            wave_num += 1
            candidates = await asyncio.to_thread(
                _collect_repo_transitive_candidates,
                parsed_by_source,
                repo,
                scope_packages=scope_packages,
            )
            to_fetch = sorted(
                c for c in candidates if c not in parsed_by_source and c not in attempted_fetch
            )
            logger.info(
                "repo transitive wave %s/%s %s/%s: candidates=%s to_fetch=%s parsed=%s",
                wave_num,
                max_waves,
                releasename,
                distgit_branch,
                len(candidates),
                len(to_fetch),
                len(parsed_by_source),
            )
            if not to_fetch:
                break

            wave_added = False
            for off in range(0, len(to_fetch), wave_size):
                wave = to_fetch[off : off + wave_size]
                batch_specs = await _fetch_distgit_specs_batch(
                    client,
                    owner=owner,
                    repo=repo_name,
                    distgit_branch=distgit_branch,
                    releasename=releasename,
                    packages=wave,
                )
                for pkg in wave:
                    if pkg not in batch_specs:
                        attempted_fetch.add(pkg)
                if not batch_specs:
                    continue
                wave_added = True
                fetched_extra += len(batch_specs)
                futures = {
                    pool.submit(parse_fn, batch_specs[pkg], f"{pkg}.spec"): pkg
                    for pkg in batch_specs
                }
                for fut in as_completed(futures):
                    pkg = futures[fut]
                    try:
                        parsed = fut.result()
                    except Exception as exc:
                        err.append(f"{pkg}: parse failed ({type(exc).__name__}: {exc})")
                        parsed = _SpecParseResult(
                            source_name=pkg,
                            build_requires=[],
                            provided_binary_names={pkg},
                            build_requires_raw=[],
                        )
                    specs[pkg] = batch_specs[pkg]
                    parsed_by_source[pkg] = parsed
            if not wave_added:
                break

    if wave_num >= max_waves and to_fetch:
        err.append(
            f"Repo transitive distgit closure hit OREON_CHAIN_REPO_TRANSITIVE_MAX_WAVES ({max_waves}); some runtime deps may be missing."
        )

    if len(parsed_by_source) > max_parse:
        err.append(
            f"Chain distgit closure exceeded OREON_CHAIN_DISTGIT_MAX_PARSE ({max_parse}); narrow the chain spec."
        )

    logger.info(
        "repo transitive distgit closure %s/%s: parsed=%s extra_fetched=%s in %.1fs",
        releasename,
        distgit_branch,
        len(parsed_by_source),
        fetched_extra,
        time.monotonic() - t0,
    )
    return specs, parsed_by_source, err


async def compute_distgit_build_order_for_chain(
    *,
    releasename: str,
    distgit_url: str = "https://github.com/oreonhq/rpm-specfiles.git",
    distgit_branch: str = "main",
    root_packages: list[str],
) -> DistgitBuildOrderResult:
    err: list[str] = []
    gr = _github_owner_repo_from_git_url(distgit_url)
    if not gr:
        return DistgitBuildOrderResult(
            releasename=releasename,
            distgit_url=distgit_url,
            distgit_branch=distgit_branch,
            package_names=[],
            edges=[],
            layers=[],
            capability_providers={},
            spec_evrs={},
            build_requires_raw={},
            errors=[f"distgit_url is not a supported github.com URL: {distgit_url!r}"],
            cycle_warning=False,
        )
    owner, repo = gr
    settings = get_settings()
    token = (getattr(settings, "github_token", None) or os.environ.get("GITHUB_TOKEN") or "").strip() or None
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    tarball, t_err = await _download_distgit_tarball(
        owner=owner, repo=repo, distgit_branch=distgit_branch, headers=headers
    )
    if t_err:
        err.append(t_err)
    if not tarball:
        if not err:
            err.append("distgit tarball fetch returned no data")
        return _distgit_order_from_parsed(
            releasename=releasename,
            distgit_url=distgit_url,
            distgit_branch=distgit_branch,
            parsed_by_source={},
            err=err,
        )

    t_index = time.monotonic()
    all_names, members = await asyncio.to_thread(
        _tarball_spec_member_index, tarball, releasename
    )
    logger.info(
        "distgit tarball index: %s packages under %s in %.1fs",
        len(all_names),
        releasename,
        time.monotonic() - t_index,
    )
    root_set: set[str] = set()
    for r in root_packages:
        rn = (r or "").strip()
        if rn:
            root_set.add(rn)
    unknown = sorted(rn for rn in root_set if rn not in all_names)
    for rn in unknown:
        err.append(f"chain root {rn!r} not found under {releasename}/ in distgit")
    found = root_set & all_names
    specs_by_pkg = _read_specs_from_tarball(
        tarball, releasename, found, members=members
    )
    logger.info(
        "distgit CI chain: %s specs from %s changed root(s) in %.1fs (no closure expansion)",
        len(specs_by_pkg),
        len(found),
        time.monotonic() - t_index,
    )
    parsed_by_source, perr = await _parse_specs_parallel_async(specs_by_pkg, releasename=releasename)
    err.extend(perr)
    return _distgit_order_from_parsed(
        releasename=releasename,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        parsed_by_source=parsed_by_source,
        err=err,
    )


async def _fetch_specs_by_tarball(
    *,
    owner: str,
    repo: str,
    releasename: str,
    distgit_branch: str,
    headers: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    errs: list[str] = []
    tarball, t_err = await _download_distgit_tarball(
        owner=owner, repo=repo, distgit_branch=distgit_branch, headers=headers
    )
    if t_err:
        return {}, [t_err]
    try:
        names = _tarball_spec_package_names(tarball, releasename)
        specs = _read_specs_from_tarball(tarball, releasename, names)
    except Exception as exc:
        return {}, [f"tarball parse failed: {type(exc).__name__}: {exc}"]
    return specs, errs


def _source_name_from_srpm_filename(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s.endswith(".src.rpm"):
        return None
    s = s[:-8]
    parts = s.rsplit("-", 2)
    if len(parts) != 3:
        return None
    return parts[0] or None


def _built_sources_from_r2(releasename: str, repo_branch: str) -> set[str]:
    r2 = get_r2_client()
    src_prefix = f"{releasename}/{repo_branch}/src/"
    out: set[str] = set()
    for key in r2.iter_keys_with_prefix(src_prefix):
        fn = key.rsplit("/", 1)[-1]
        src = _source_name_from_srpm_filename(fn)
        if src:
            out.add(src)
    return out


async def compute_distgit_build_order(
    *,
    releasename: str,
    distgit_url: str = "https://github.com/oreonhq/rpm-specfiles.git",
    distgit_branch: str = "main",
) -> DistgitBuildOrderResult:
    err: list[str] = []
    gr = _github_owner_repo_from_git_url(distgit_url)
    if not gr:
        return DistgitBuildOrderResult(
            releasename=releasename,
            distgit_url=distgit_url,
            distgit_branch=distgit_branch,
            package_names=[],
            edges=[],
            layers=[],
            capability_providers={},
            spec_evrs={},
            build_requires_raw={},
            errors=[f"distgit_url is not a supported github.com URL: {distgit_url!r}"],
            cycle_warning=False,
        )
    owner, repo = gr
    settings = get_settings()
    token = (getattr(settings, "github_token", None) or os.environ.get("GITHUB_TOKEN") or "").strip() or None
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    specs_by_pkg, t_err = await _fetch_specs_by_tarball(
        owner=owner,
        repo=repo,
        releasename=releasename,
        distgit_branch=distgit_branch,
        headers=headers,
    )
    err.extend(t_err)
    if not specs_by_pkg and not err:
        err.append(f"No packages with specs found under {releasename}/ on branch {distgit_branch}")

    parsed_by_source, perr = await _parse_specs_parallel_async(specs_by_pkg, releasename=releasename)
    err.extend(perr)
    return _distgit_order_from_parsed(
        releasename=releasename,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        parsed_by_source=parsed_by_source,
        err=err,
    )


def _sources_from_spec_capability_map(
    capability: str,
    prov_map: dict[str, list[str]],
    spec_set: set[str],
) -> set[str]:
    cap = (capability or "").strip()
    if not cap:
        return set()
    found: set[str] = set()
    if cap in spec_set:
        found.add(cap)
    hit = prov_map.get(cap)
    if hit:
        found.update(s for s in hit if s in spec_set)
    cap_low = cap.lower()
    for key, srcs in prov_map.items():
        if key.lower() == cap_low:
            found.update(s for s in srcs if s in spec_set)
    return found


def resolve_distgit_sources_for_capability(
    order: DistgitBuildOrderResult,
    capability: str,
    *,
    repo_provides_index: dict[str, set[str]] | None = None,
    repo_known_sources: set[str] | None = None,
) -> list[str]:
    cap = (capability or "").strip()
    if not cap:
        return []
    spec_set = set(order.package_names)
    prov_map = order.capability_providers or {}
    found = _sources_from_spec_capability_map(cap, prov_map, spec_set)

    if repo_provides_index:
        providers = repo_provides_index.get(cap)
        if not providers:
            cap_low = cap.lower()
            for key, srcs in repo_provides_index.items():
                if key.lower() == cap_low:
                    providers = srcs
                    break
        if providers:
            found.update(s for s in providers if s in spec_set)

    return sorted(found)


async def compute_spec_tree_dependency_plan(
    *,
    releasename: str,
    repo_branch: str = "dev",
    existing_only: bool = False,
    distgit_url: str = "https://github.com/oreonhq/rpm-specfiles.git",
    distgit_branch: str = "main",
) -> SpecTreeResolveResult:
    errors: list[str] = []
    gr = _github_owner_repo_from_git_url(distgit_url)
    if not gr:
        return SpecTreeResolveResult(
            releasename=releasename,
            repo_branch=repo_branch,
            distgit_url=distgit_url,
            distgit_branch=distgit_branch,
            mode="existing_only" if existing_only else "all_missing",
            total_spec_packages=0,
            built_sources=[],
            never_built_sources=[],
            selected_for_chain=[],
            required_missing_sources=[],
            edges=[],
            layers=[],
            chain_string="",
            unresolved_requirements=[],
            errors=[f"distgit_url is not a supported github.com URL: {distgit_url!r}"],
            cycle_warning=False,
        )
    owner, repo = gr
    settings = get_settings()
    token = (getattr(settings, "github_token", None) or os.environ.get("GITHUB_TOKEN") or "").strip() or None
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    specs_by_pkg, t_err = await _fetch_specs_by_tarball(
        owner=owner,
        repo=repo,
        releasename=releasename,
        distgit_branch=distgit_branch,
        headers=headers,
    )
    errors.extend(t_err)
    names_set = set(specs_by_pkg.keys())
    built_sources = _built_sources_from_r2(releasename, repo_branch)
    never_built = sorted(n for n in names_set if n not in built_sources)

    loop = asyncio.get_running_loop()
    pool = _distgit_spec_parse_executor()
    parse_fn = _spec_parse_fn(releasename)
    tasks = [
        loop.run_in_executor(pool, parse_fn, spec_text, f"{pkg}.spec")
        for pkg, spec_text in specs_by_pkg.items()
    ]
    parse_results = await asyncio.gather(*tasks, return_exceptions=True)
    parsed_by_source: dict[str, _SpecParseResult] = {}
    unresolved: list[dict[str, str]] = []
    unresolved_seen: set[tuple[str, str]] = set()
    for (pkg, _spec_text), parsed_or_exc in zip(specs_by_pkg.items(), parse_results, strict=False):
        if isinstance(parsed_or_exc, Exception):
            errors.append(f"{pkg}: parse failed ({type(parsed_or_exc).__name__}: {parsed_or_exc})")
            parsed_by_source[pkg] = _SpecParseResult(source_name=pkg, build_requires=[], provided_binary_names={pkg})
        else:
            parsed_by_source[pkg] = parsed_or_exc

    provider_map = _provider_map_from_specs(parsed_by_source)

    deps_all: dict[str, set[str]] = {n: set() for n in names_set}
    rdeps_all: dict[str, set[str]] = {n: set() for n in names_set}
    for cons, parsed in parsed_by_source.items():
        for dep in parsed.dependency_names():
            providers = provider_map.get(dep) or set()
            valid_providers = [p for p in providers if p in names_set and p != cons]
            if not valid_providers:
                if br not in names_set:
                    key = (cons, br)
                    if key not in unresolved_seen:
                        unresolved_seen.add(key)
                        unresolved.append({"package": cons, "requirement": br})
                continue
            for prov in valid_providers:
                deps_all[cons].add(prov)
                rdeps_all[prov].add(cons)

    needed_missing: set[str] = set()
    if existing_only:
        frontier = [p for p in built_sources if p in names_set]
        seen: set[str] = set(frontier)
        while frontier:
            cur = frontier.pop()
            for dep in deps_all.get(cur, set()):
                if dep in built_sources:
                    continue
                if dep not in needed_missing:
                    needed_missing.add(dep)
                if dep not in seen:
                    seen.add(dep)
                    frontier.append(dep)
        selected = set(needed_missing)
    else:
        selected = {p for p in names_set if p not in built_sources}
        needed_missing = set(selected)

    deps_sel: dict[str, set[str]] = {n: set() for n in selected}
    rdeps_sel: dict[str, set[str]] = {n: set() for n in selected}
    edges: list[dict[str, str]] = []
    for cons in selected:
        for dep in deps_all.get(cons, set()):
            if dep not in selected:
                continue
            deps_sel[cons].add(dep)
            rdeps_sel[dep].add(cons)
            edges.append({"from": cons, "to": dep})

    layers, cyc = _kahn_layers(selected, deps_sel, rdeps_sel) if selected else ([], False)
    chain = " : ".join(" ".join(layer) for layer in layers if layer)
    return SpecTreeResolveResult(
        releasename=releasename,
        repo_branch=repo_branch,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        mode="existing_only" if existing_only else "all_missing",
        total_spec_packages=len(names_set),
        built_sources=sorted(p for p in built_sources if p in names_set),
        never_built_sources=never_built,
        selected_for_chain=sorted(selected),
        required_missing_sources=sorted(needed_missing),
        edges=edges,
        layers=layers,
        chain_string=chain,
        unresolved_requirements=sorted(unresolved, key=lambda x: (x["package"], x["requirement"])),
        errors=errors,
        cycle_warning=cyc,
    )


async def compute_unbuilt_spec_list(
    *,
    releasename: str,
    db_package_names: set[str],
    distgit_url: str = "https://github.com/oreonhq/rpm-specfiles.git",
    distgit_branch: str = "main",
) -> UnbuiltSpecListResult:
    errors: list[str] = []
    gr = _github_owner_repo_from_git_url(distgit_url)
    if not gr:
        return UnbuiltSpecListResult(
            releasename=releasename,
            distgit_url=distgit_url,
            distgit_branch=distgit_branch,
            total_spec_packages=0,
            total_db_packages=len(db_package_names),
            unbuilt_packages=[],
            errors=[f"distgit_url is not a supported github.com URL: {distgit_url!r}"],
        )

    owner, repo = gr
    settings = get_settings()
    token = (getattr(settings, "github_token", None) or os.environ.get("GITHUB_TOKEN") or "").strip() or None
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    specs_by_pkg, tar_err = await _fetch_specs_by_tarball(
        owner=owner,
        repo=repo,
        releasename=releasename,
        distgit_branch=distgit_branch,
        headers=headers,
    )
    errors.extend(tar_err)
    spec_names = set(specs_by_pkg.keys())
    unbuilt = sorted(name for name in spec_names if name not in db_package_names)

    return UnbuiltSpecListResult(
        releasename=releasename,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        total_spec_packages=len(spec_names),
        total_db_packages=len(db_package_names),
        unbuilt_packages=unbuilt,
        errors=errors,
    )
