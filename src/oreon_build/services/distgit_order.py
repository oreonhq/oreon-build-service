from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import tarfile
import time
import urllib.request
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
    _expand_br_macros,
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
    exclusive_arches: set[str] | None = None
    has_generate_buildrequires: bool = False
    # authoritative when set (rpmspec / rpmbuild -br). else use bcond parse.
    rpm_build_requires: list[str] | None = None
    # False when %generate_buildrequires eval failed. None = not attempted.
    dyn_br_complete: bool | None = None

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
_RE_PERL_MODULE_FILE = re.compile(
    r"(?:/perl5/|/perl/)(?:vendor_perl/|core_perl/|site_perl/)?((?:[^/\s]+/)*[^/\s]+\.pm)"
)
_RE_CMAKE_MODULE_FILE = re.compile(r"/cmake/([^/%*\s]+)")
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
    # honor %{?foo:bar} so Version: %{up_version}%{?up_prever:...} actually expands
    return _expand_br_macros(value or "", macros, max_rounds=max_rounds)


def _extract_br_tokens_from_tag_value(value: str) -> list[str]:
    return rpm_requirement_capabilities_from_value(value)


def _caps_from_raw_and_names(raw_list: list[str], names: list[str]) -> list[str]:
    caps: list[str] = []
    seen: set[str] = set()

    def add(cap: str) -> None:
        c = (cap or "").strip()
        if not c or c in seen or "%" in c:
            return
        seen.add(c)
        caps.append(c)

    for raw in raw_list:
        for cap in rpm_requirement_tokens_from_value(raw):
            add(cap)
    for name in names:
        add(name)
    return caps


def build_dependency_capabilities(parsed: _SpecParseResult) -> list[str]:
    return _caps_from_raw_and_names(
        list(parsed.build_requires_raw or []),
        list(parsed.build_requires or []),
    )


def runtime_dependency_capabilities(parsed: _SpecParseResult) -> list[str]:
    return _caps_from_raw_and_names(
        list(parsed.requires_raw or []),
        list(parsed.requires or []),
    )


def dependency_capabilities(parsed: _SpecParseResult) -> list[str]:
    caps: list[str] = []
    seen: set[str] = set()
    for cap in build_dependency_capabilities(parsed) + runtime_dependency_capabilities(parsed):
        if cap in seen:
            continue
        seen.add(cap)
        caps.append(cap)
    return caps


def iter_plan_capabilities(
    parsed: _SpecParseResult,
    bcond: Any,
    repo: Any,
    defines: dict[str, str] | None = None,
) -> list[str]:
    use_defines = defines if defines is not None else default_build_defines(bcond)
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
    for br in active_build_brs(bcond, use_defines):
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
    exclusive_arches: set[str] | None = None
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
        if us.startswith("EXCLUSIVEARCH:"):
            val = _expand_rpm_macros(s.split(":", 1)[1].strip(), macros)
            toks = {
                t.lower()
                for t in re.split(r"[\s,]+", val)
                if t and "%" not in t and t.lower() not in {"noarch"}
            }
            if toks:
                exclusive_arches = (exclusive_arches or set()) | toks
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
    for name in (source_name,):
        perl_cap = _perl_provide_from_package_name(name)
        if perl_cap:
            provided.add(perl_cap)
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
                if "%" not in m.group(0) and "*" not in m.group(0):
                    provided.add(m.group(0))
    macros["name"] = source_name
    from oreon_build.services.dynamic_brs import spec_has_generate_buildrequires

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
        exclusive_arches=exclusive_arches,
        has_generate_buildrequires=spec_has_generate_buildrequires(spec_text),
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
    """Layer sources with networkx topological_generations (provider before consumer).

    deps[cons] = providers that must build first. On cycles, break like before
    (lowest remaining indegree, prefer high fan-out providers over consumers).
    """
    import networkx as nx

    if not names:
        return [], False

    G = nx.DiGraph()
    G.add_nodes_from(names)
    for cons, provs in deps.items():
        if cons not in names:
            continue
        for p in provs:
            if p in names and p != cons:
                G.add_edge(p, cons)

    cycle = False
    layers: list[list[str]] = []
    remaining = set(names)
    while remaining:
        sub = G.subgraph(remaining)
        try:
            for gen in nx.topological_generations(sub):
                layers.append(sorted(gen))
            return layers, cycle
        except nx.NetworkXUnfeasible:
            cycle = True
            in_deg = {n: int(sub.in_degree(n)) for n in remaining}
            pick = min(
                remaining,
                key=lambda n: (
                    in_deg.get(n, 0),
                    -sum(1 for d in rdeps.get(n, set()) if d in remaining),
                    n,
                ),
            )
            layers.append([pick])
            remaining.discard(pick)
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
_PROVIDES_INDEX_MEM_TTL_SEC = 1800.0
_tarball_cache: dict[tuple[str, str, str, str], tuple[float, bytes]] = {}


def _prune_tarball_cache(now: float | None = None) -> None:
    ts_now = time.monotonic() if now is None else now
    for k, (ts, _blob) in list(_tarball_cache.items()):
        if ts_now - ts >= _TARBALL_CACHE_TTL_SEC:
            _tarball_cache.pop(k, None)


def _prune_provides_index_cache(now: float | None = None) -> None:
    """Drop expired indexes and keep only the newest tree SHA per branch/release."""
    ts_now = time.monotonic() if now is None else now
    for k, (ts, _idx) in list(_PROVIDES_INDEX_CACHE.items()):
        if ts_now - ts >= _PROVIDES_INDEX_MEM_TTL_SEC:
            _PROVIDES_INDEX_CACHE.pop(k, None)
    newest: dict[tuple[str, str, str, str], tuple[float, tuple]] = {}
    for k, (ts, _idx) in list(_PROVIDES_INDEX_CACHE.items()):
        base = (k[0], k[1], k[2], k[3])
        prev = newest.get(base)
        if prev is None or ts >= prev[0]:
            newest[base] = (ts, k)
    keep = {v[1] for v in newest.values()}
    for k in list(_PROVIDES_INDEX_CACHE):
        if k not in keep:
            _PROVIDES_INDEX_CACHE.pop(k, None)


def prune_distgit_memory_caches() -> None:
    _prune_tarball_cache()
    _prune_provides_index_cache()
    now = time.monotonic()
    for k, (ts, _names) in list(_PACKAGE_NAMES_CACHE.items()):
        if now - ts >= _PACKAGE_NAMES_CACHE_TTL_SEC:
            _PACKAGE_NAMES_CACHE.pop(k, None)


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
    _prune_tarball_cache(now)
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
        # only keep the latest tarball for this branch
        for k in list(_tarball_cache):
            if k[:3] == (owner, repo, distgit_branch) and k != key:
                _tarball_cache.pop(k, None)
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


_PERL_SUBDIRS = ("vendor_perl", "core_perl", "site_perl")


def _perl_module_from_file_path(path: str) -> str | None:
    """Derive perl(Module::Name) from a Perl module file path."""
    if not path:
        return None
    m = _RE_PERL_MODULE_FILE.search(path)
    if not m:
        return None
    rel = m.group(1)
    if "%" in rel or "*" in rel:
        return None
    for subdir in _PERL_SUBDIRS:
        if rel.startswith(f"{subdir}/"):
            rel = rel[len(f"{subdir}/"):]
            break
    if not rel or not rel.endswith(".pm"):
        return None
    module = rel[:-3].replace("/", "::")
    if not module or module.startswith("::"):
        return None
    return f"perl({module})"


def _cmake_module_from_file_path(path: str) -> str | None:
    """Derive cmake(project) from a CMake module file path."""
    if not path:
        return None
    m = _RE_CMAKE_MODULE_FILE.search(path)
    if not m:
        return None
    name = m.group(1)
    if not name or "%" in name or "*" in name:
        return None
    # bson- from failed %{version} expand is junk
    if name.endswith("-") or name.startswith("-"):
        return None
    return f"cmake({name})"


_RE_MINGW_DLL_FILE = re.compile(
    r"/(?:usr/)?(?P<arch>i686|x86_64)-w64-mingw32/sys-root/mingw/bin/"
    r"(?P<dll>lib[A-Za-z0-9_+.-]+\.dll)\b",
    re.IGNORECASE,
)
_RE_MINGW_DLL_MACRO = re.compile(
    r"%\{?mingw(?P<bits>32|64)_bindir\}?/(?P<dll>lib[A-Za-z0-9_+.-]+\.dll)\b",
    re.IGNORECASE,
)


def _mingw_dll_provides_from_path(path: str) -> set[str]:
    """mingw32(libfoo.dll) from %files dll paths."""
    raw = path or ""
    if not raw:
        return set()
    out: set[str] = set()
    m = _RE_MINGW_DLL_MACRO.search(raw)
    if m:
        out.add(f"mingw{m.group('bits')}({m.group('dll')})")
        return out
    m = _RE_MINGW_DLL_FILE.search(raw)
    if m:
        bits = "32" if m.group("arch") == "i686" else "64"
        out.add(f"mingw{bits}({m.group('dll')})")
    return out


def _python_dist_provides_from_package_name(pkg: str) -> set[str]:
    """python3-foo-bar / python-foo -> python3dist(foo-bar) (+ underscore form)."""
    n = (pkg or "").strip()
    out: set[str] = set()
    for prefix in ("python3-", "python-"):
        if not n.startswith(prefix) or len(n) <= len(prefix):
            continue
        rest = n[len(prefix) :]
        if not rest or rest[0].isdigit():
            continue
        low = rest.lower()
        if low in {"devel", "libs", "pip", "setuptools-wheel", "rpm-macros"}:
            # still invent for setuptools itself; skip only pure tooling splits that never
            # match python3dist(name) from py3_dist
            pass
        if low.endswith(("-debuginfo", "-debugsource")):
            continue
        out.add(f"python3dist({rest})")
        dashed = rest.replace("_", "-")
        unders = rest.replace("-", "_")
        out.add(f"python3dist({dashed})")
        out.add(f"python3dist({unders})")
        out.add(f"python3dist({rest.lower()})")
        out.add(f"python3dist({dashed.lower()})")
        out.add(f"python3dist({unders.lower()})")
        break
    return out


def _rust_crate_name_from_package(pkg: str, version: str | None = None) -> str | None:
    """Map rust2rpm source dir to crate name.

    rust-itertools0.13 + Version 0.13.0 → itertools
    rust-nom7 + Version 7.1.3 → nom
    rust-sha1 + Version 0.10.1 → sha1 (digit crate name, no false strip)
    rust-serde_json → serde_json
    """
    n = (pkg or "").strip()
    if not n.startswith("rust-") or len(n) <= 5:
        return None
    rest = n[5:]
    if not rest or rest[0].isdigit() or "+" in rest:
        return None
    low = rest.lower()
    if low in {
        "packaging",
        "srpm-macros",
        "rpm-macros",
        "toolset",
        "std-static",
        "debugger",
        "doc",
        "analyzer",
    }:
        return None
    if low.endswith(("-debuginfo", "-debugsource", "-tests")):
        return None
    ver = (version or "").strip()
    if ver:
        parts = [p for p in ver.split(".") if p != ""]
        candidates: list[str] = []
        # rust2rpm compat dirs append 0.minor (and sometimes major.minor.patch-ish)
        if len(parts) >= 2 and parts[0] == "0":
            candidates.append(f"{parts[0]}.{parts[1]}")
        if parts:
            candidates.append(parts[0])
        for c in candidates:
            if not c or not rest.endswith(c):
                continue
            if len(rest) <= len(c):
                continue
            base = rest[: -len(c)]
            if base and re.search(r"[A-Za-z_]", base):
                return base
    # no Version: still strip obvious 0.x compat suffixes (never part of a real crate name)
    m = re.match(r"^(?P<crate>.+?)(?P<ver>0\.\d+(?:\.\d+)*)$", rest)
    if m and re.search(r"[A-Za-z_]", m.group("crate") or ""):
        return m.group("crate")
    return rest


def _crate_provides_from_package_name(
    pkg: str, *, version: str | None = None
) -> set[str]:
    """rust-foo_bar / rust-foo0.13 (rust2rpm) → crate(foo_bar) / crate(foo)."""
    crate = _rust_crate_name_from_package(pkg, version=version)
    if not crate:
        return set()
    return {f"crate({crate})"}


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
                for py_cap in _python_dist_provides_from_package_name(pkg):
                    raw.setdefault(py_cap, set()).add(pkg)
                for crate_cap in _crate_provides_from_package_name(pkg):
                    raw.setdefault(crate_cap, set()).add(pkg)
            self._indexed = index_provider_map(raw)
        return self._indexed


_PROVIDES_INDEX_CACHE: dict[tuple[str, str, str, str, str], tuple[float, DistgitProvidesIndex]] = {}
_provides_index_tasks: dict[tuple[str, str, str, str, str], asyncio.Task[DistgitProvidesIndex | None]] = {}
_PACKAGE_NAMES_CACHE: dict[tuple[str, str, str, str], tuple[float, frozenset[str]]] = {}
_PACKAGE_NAMES_CACHE_TTL_SEC = 1800.0
_PROVIDES_INDEX_DISK_VERSION = "v13"


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
    macros.setdefault("_bindir", "/usr/bin")
    macros.setdefault("_sbindir", "/usr/sbin")
    macros.setdefault("_libexecdir", "/usr/libexec")
    macros.setdefault("_libdir", "/usr/lib64")
    macros.setdefault("_datadir", "/usr/share")
    macros.setdefault("_prefix", "/usr")
    macros.setdefault(
        "mingw32_bindir", "/usr/i686-w64-mingw32/sys-root/mingw/bin"
    )
    macros.setdefault(
        "mingw64_bindir", "/usr/x86_64-w64-mingw32/sys-root/mingw/bin"
    )
    macros.setdefault("mingw32_libdir", "/usr/i686-w64-mingw32/sys-root/mingw/lib")
    macros.setdefault("mingw64_libdir", "/usr/x86_64-w64-mingw32/sys-root/mingw/lib")
    # honor %bcond / %if so compat packages keep llvm15 not llvm from the %else branch
    bcond = parse_spec_bconds(spec_text, distro_macros=macros)
    macros.update(bcond.macros)
    macros.setdefault("name", pkg)
    rpm_version = (macros.get("version") or "").strip()
    goipath = (macros.get("goipath") or "").strip()
    if goipath and "%" not in goipath:
        provided.add(f"golang({goipath})")
        provided.add(f"__goprefix__{goipath}")
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
        if us.startswith("VERSION:"):
            ver = _expand_rpm_macros((s.split(":", 1)[1] or "").strip(), macros)
            if ver and "%" not in ver:
                rpm_version = ver
                macros["version"] = ver
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
        if low.startswith("%ghc_lib_subpackage"):
            rest = s.split(None, 1)[1].strip() if len(s.split(None, 1)) > 1 else ""
            rest = _expand_rpm_macros(rest, macros)
            tokens = rest.split()
            i = 0
            while i < len(tokens):
                tok = tokens[i]
                if tok == "-l":
                    i += 2
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                if "%" not in tok:
                    lm = re.match(r"^([A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*)-\d[\w.]*$", tok)
                    libname = lm.group(1) if lm else tok
                    provided.add(f"ghc-libhs({libname})")
                i += 1
            continue
        if low.startswith("%py_provides") or low.startswith("%py3_provides"):
            rest = s.split(None, 1)[1].strip() if len(s.split(None, 1)) > 1 else ""
            rest = _expand_rpm_macros(rest, macros).split()[0] if rest else ""
            if rest and "%" not in rest:
                provided.add(rest)
                provided.update(_python_dist_provides_from_package_name(rest))
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
            elif "*" in expanded and "/pkgconfig/" in expanded:
                # Fcitx5*.pc → at least pkgconfig(Fcitx5) prefix for map hits
                gm = re.search(r"/pkgconfig/([A-Za-z0-9_+.-]+)\*\.pc\b", expanded)
                if gm and gm.group(1):
                    provided.add(f"pkgconfig({gm.group(1)})")
        if ".so" in expanded:
            m = _RE_SO_FILE.search(expanded)
            if m and "%" not in m.group(1):
                provided.update(_so_file_provides(m.group(1)))
                if "%" not in m.group(0) and "*" not in m.group(0):
                    provided.add(m.group(0))
            elif "%" not in expanded:
                gm = re.search(r"/\*\.so\.([0-9][0-9.]*)\*?\s*$", expanded)
                if gm:
                    pkgname = (macros.get("name") or "").strip()
                    if pkgname.startswith("lib"):
                        provided.add(f"{pkgname}.so.{gm.group(1)}")
        if ".pm" in expanded:
            perl_cap = _perl_module_from_file_path(expanded)
            if perl_cap:
                provided.add(perl_cap)
        if "/cmake/" in expanded:
            cmake_cap = _cmake_module_from_file_path(expanded)
            if cmake_cap:
                provided.add(cmake_cap)
        if ".dll" in expanded.lower() or "mingw" in expanded.lower():
            provided.update(_mingw_dll_provides_from_path(expanded))
            provided.update(_mingw_dll_provides_from_path(s))
        if "%" not in expanded:
            path = expanded.split()[0] if expanded.split() else ""
            if path.startswith("/") and "*" not in path and "?" not in path and "[" not in path:
                provided.add(path)
                base = path.rsplit("/", 1)[-1]
                if base and "." not in base:
                    provided.add(base)
            elif path.startswith("/usr/bin/") and path.endswith("-*"):
                stem = path[len("/usr/bin/") : -2]
                if stem and "%" not in stem:
                    provided.add(f"/usr/bin/{stem}")
                    provided.add(stem)
            elif path.startswith("/") and any(c in path for c in "*?["):
                provided.add(f"__glob__{path}")
    provided.add(source_name)
    crate_macro = (macros.get("crate") or "").strip()
    if crate_macro and "%" not in crate_macro:
        provided.add(f"crate({crate_macro})")
    else:
        provided.update(
            _crate_provides_from_package_name(pkg, version=rpm_version or None)
        )
        if source_name != pkg:
            provided.update(
                _crate_provides_from_package_name(
                    source_name, version=rpm_version or None
                )
            )
    for name in (pkg, source_name):
        perl_cap = _perl_provide_from_package_name(name)
        if perl_cap:
            provided.add(perl_cap)
        provided.update(_python_dist_provides_from_package_name(name))
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
            # full spec: texlive-collection-* is multi-MB with thousands of %package -n
            text = f.read().decode("utf-8", errors="replace")
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
    _prune_provides_index_cache(now)
    hit = _PROVIDES_INDEX_CACHE.get(key)
    if hit and now - hit[0] < _PROVIDES_INDEX_MEM_TTL_SEC:
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
    raw = (os.getenv("OREON_CHAIN_DISTGIT_PARSE_WAVE") or "96").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 96
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



def _enrich_one_spec_brs(
    pkg: str,
    spec_text: str,
    parsed: _SpecParseResult,
    *,
    distro_macros: dict[str, str] | None,
    run_generate: bool,
    local_source_paths: list[Path] | None = None,
) -> _SpecParseResult:
    """Merge rpmspec / rpmbuild -br BRs into the parse result."""
    from oreon_build.core.spec_bcond import default_build_defines, parse_spec_bconds
    from oreon_build.services.dynamic_brs import (
        evaluate_generate_buildrequires,
        merge_buildrequires,
        rpmspec_query_buildrequires,
        spec_has_generate_buildrequires,
    )

    bcond = parse_spec_bconds(spec_text, distro_macros=distro_macros)
    defines = default_build_defines(bcond)
    has_gen = parsed.has_generate_buildrequires or spec_has_generate_buildrequires(spec_text)
    try:
        if run_generate and has_gen:
            dyn = evaluate_generate_buildrequires(
                spec_text,
                defines=defines,
                local_source_paths=local_source_paths,
            )
            rpm_brs = list(dyn.buildrequires)
            parsed.dyn_br_complete = bool(dyn.complete)
        else:
            rpm_brs = rpmspec_query_buildrequires(spec_text, defines=defines)
            if has_gen and not run_generate:
                parsed.dyn_br_complete = None
            else:
                parsed.dyn_br_complete = True
    except Exception:
        logger.warning("rpm BR enrich failed for %s", pkg, exc_info=True)
        if run_generate and has_gen:
            parsed.dyn_br_complete = False
        return parsed
    if not rpm_brs and not (run_generate and has_gen and parsed.dyn_br_complete is False):
        return parsed
    merged = merge_buildrequires(parsed.build_requires, rpm_brs)
    parsed.build_requires = merged
    parsed.build_requires_raw = merge_buildrequires(parsed.build_requires_raw or [], rpm_brs)
    parsed.rpm_build_requires = list(rpm_brs)
    parsed.has_generate_buildrequires = has_gen
    return parsed


_LOCAL_SOURCE_FAIL_CACHE: dict[str, float] = {}
_LOCAL_SOURCE_FAIL_TTL_SEC = 15 * 60


def _local_source_fail_cached(url: str) -> bool:
    ts = _LOCAL_SOURCE_FAIL_CACHE.get(url)
    if ts is None:
        return False
    if time.monotonic() - ts > _LOCAL_SOURCE_FAIL_TTL_SEC:
        _LOCAL_SOURCE_FAIL_CACHE.pop(url, None)
        return False
    return True


def _local_source_mark_failed(url: str) -> None:
    _LOCAL_SOURCE_FAIL_CACHE[url] = time.monotonic()


def _fetch_one_local_source(
    name: str, url: str, dest: Path, headers: dict[str, str], pkg: str
) -> Path | None:
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    if _local_source_fail_cached(url):
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "oreon-build-service/dynamic-brs",
                **{k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"},
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        logger.warning("empty distgit source %s for %s", name, pkg)
    except Exception:
        logger.debug("distgit source fetch failed %s/%s", pkg, name, exc_info=True)
    _local_source_mark_failed(url)
    return None


def _download_distgit_local_sources(
    *,
    pkg: str,
    spec_text: str,
    releasename: str,
    owner: str,
    repo: str,
    distgit_branch: str,
    headers: dict[str, str],
    distro_macros: dict[str, str] | None,
    dest_dir: Path,
) -> list[Path]:
    """Pull non-URL SourceN / PatchN files from the package dir in distgit.

    Fetches in parallel and skips URLs that recently 404'd (never-block on
    the same permanently-missing patch every fixed-point wave).
    """
    from oreon_build.core.spec_bcond import default_build_defines, parse_spec_bconds
    from oreon_build.services.dynamic_brs import local_source_basenames

    bcond = parse_spec_bconds(spec_text, distro_macros=distro_macros)
    defines = default_build_defines(bcond)
    names = local_source_basenames(spec_text, defines)
    if not names:
        return []
    dest_dir.mkdir(parents=True, exist_ok=True)
    base = (
        f"https://raw.githubusercontent.com/{owner}/{repo}/"
        f"{distgit_branch}/{releasename}/{pkg}"
    )
    jobs = [(name, f"{base}/{name}", dest_dir / name) for name in names]
    out: list[Path] = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as pool:
        futs = {
            pool.submit(_fetch_one_local_source, name, url, dest, headers, pkg): name
            for name, url, dest in jobs
        }
        for fut in as_completed(futs):
            result = fut.result()
            if result is not None:
                out.append(result)
    return out


async def enrich_specs_buildrequires_with_rpm(
    specs_by_pkg: dict[str, str],
    parsed_by_source: dict[str, _SpecParseResult],
    *,
    releasename: str,
    scope_packages: set[str] | None = None,
    generate_packages: set[str] | None = None,
    owner: str | None = None,
    repo: str | None = None,
    distgit_branch: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """In-place: set rpm_build_requires via rpmspec / rpmbuild -br for scoped pkgs.

    rpmspec for everyone. rpmbuild -br (+ Source download + distgit locals) for generate pkgs.
    """
    from oreon_build.core.spec_bcond import default_distro_macros

    macros = default_distro_macros(releasename)
    want = set(scope_packages) if scope_packages is not None else set(parsed_by_source)
    gen = set(generate_packages or ())
    jobs = [
        (pkg, specs_by_pkg.get(pkg) or "", parsed_by_source[pkg], pkg in gen)
        for pkg in sorted(want)
        if pkg in parsed_by_source and (specs_by_pkg.get(pkg) or "")
    ]
    if not jobs:
        return

    local_by_pkg: dict[str, list[Path]] = {}
    tmp_root = None
    if gen and owner and repo and distgit_branch:
        tmp_root = tempfile.TemporaryDirectory(prefix="oreon-dynsrc-")

        async def _fetch_for(pkg: str, text: str) -> tuple[str, list[Path]]:
            dest = Path(tmp_root.name) / pkg
            paths = await asyncio.to_thread(
                _download_distgit_local_sources,
                pkg=pkg,
                spec_text=text,
                releasename=releasename,
                owner=owner,
                repo=repo,
                distgit_branch=distgit_branch,
                headers=headers or {},
                distro_macros=macros,
                dest_dir=dest,
            )
            return pkg, (paths or [])

        gen_jobs = [(pkg, text) for pkg, text, _parsed, run_gen in jobs if run_gen]
        if gen_jobs:
            for pkg, paths in await asyncio.gather(
                *(_fetch_for(pkg, text) for pkg, text in gen_jobs)
            ):
                if paths:
                    local_by_pkg[pkg] = paths

    pool = _distgit_spec_parse_executor()
    futs = {
        pool.submit(
            _enrich_one_spec_brs,
            pkg,
            text,
            parsed,
            distro_macros=macros,
            run_generate=run_gen,
            local_source_paths=local_by_pkg.get(pkg),
        ): pkg
        for pkg, text, parsed, run_gen in jobs
    }
    try:
        for fut in as_completed(futs):
            pkg = futs[fut]
            try:
                parsed_by_source[pkg] = fut.result()
            except Exception:
                logger.warning("BR enrich worker failed for %s", pkg, exc_info=True)
    finally:
        if tmp_root is not None:
            tmp_root.cleanup()


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
    distgit_providers: dict[str, set[str]] | None = None,
    always_build: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, _SpecParseResult], list[str]]:
    err: list[str] = []
    specs: dict[str, str] = {}
    parsed_by_source: dict[str, _SpecParseResult] = {}
    provider_map: dict[str, set[str]] = index_provider_map({})
    if distgit_providers:
        provider_map = merge_provider_maps(provider_map, distgit_providers)
    known: set[str] = set()
    needed: set[str] = set()
    queue: deque[str] = deque()
    force_build = {(x or "").strip() for x in (always_build or roots or []) if (x or "").strip()}

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
                # repo-current transitive pkgs: keep parse for provides, do not fan out
                if (
                    pkg not in force_build
                    and repo_index is not None
                    and hasattr(repo_index, "source_is_repo_current")
                    and repo_index.source_is_repo_current(
                        pkg,
                        getattr(parsed, "epoch", 0) or 0,
                        getattr(parsed, "version", "") or "",
                        getattr(parsed, "release", "") or "",
                    )
                ):
                    continue
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
        path_miss = getattr(repo, "missing_caps_on_provider_path", None)
        if callable(path_miss):
            misses = set(path_miss(walk_cap) or ())
        else:
            misses = set(repo.transitive_unsatisfied(walk_cap))
        if not misses and not repo.satisfies_requirement(walk_cap):
            misses = set(repo.transitive_unsatisfied(walk_cap)) or {walk_cap}
        for miss in misses:
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
    solver: Any | None = None,
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
    missing_caps: set[str] = set()
    br_fp_cache: dict[tuple[str, ...], Any] = {}

    if solver is not None:
        for _pkg, parsed in pkg_iter:
            leaf_ok: list[str] = []
            for cap in dependency_capabilities(parsed):
                if "%" in cap:
                    continue
                if repo.satisfies_requirement(cap):
                    leaf_ok.append(cap)
                else:
                    missing_caps.add(cap)
            if not leaf_ok:
                continue
            key = tuple(sorted(set(leaf_ok)))
            result = br_fp_cache.get(key)
            if result is None:
                result = solver.resolve(key)
                br_fp_cache[key] = result
            if not result.ok:
                missing_caps.update(result.missing_provides or key)
    else:
        seen_brs: set[str] = set()
        for _pkg, parsed in pkg_iter:
            for cap in dependency_capabilities(parsed):
                if "%" in cap or cap in seen_brs:
                    continue
                seen_brs.add(cap)
                # leaf holes only. pathwalk fans the whole distro into candidates.
                if not repo.satisfies_requirement(cap):
                    missing_caps.add(cap)

    for miss in missing_caps:
        candidates.update(
            sources_for_requirement(
                miss,
                repo,
                provider_maps=provider_maps,
            )
        )
        for src in _providers_from_maps(miss, provider_maps):
            if src and is_distgit_source_name(src):
                candidates.add(src)

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
        if now - ts >= _PROVIDES_INDEX_MEM_TTL_SEC:
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
            if now - ts < _PROVIDES_INDEX_MEM_TTL_SEC:
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
    solver: Any | None = None,
) -> set[str]:
    out: set[str] = set()
    chain_providers = _provider_map_from_specs(parsed_by_source)
    pkg_iter = (
        sorted(scope_packages)
        if scope_packages is not None
        else sorted(parsed_by_source.keys())
    )
    misses: set[str] = set()
    br_fp_cache: dict[tuple[str, ...], Any] = {}

    if solver is not None:
        for pkg in pkg_iter:
            if pkg not in parsed_by_source:
                continue
            spec_text = specs_by_pkg.get(pkg) or ""
            if not spec_text:
                continue
            bcond = parse_spec_bconds(
                spec_text, distro_macros=distro_macros, target_arches=target_arches
            )
            leaf_ok: list[str] = []
            for cap in iter_plan_capabilities(parsed_by_source[pkg], bcond, repo):
                if "%" in cap:
                    continue
                if repo.satisfies_requirement(cap):
                    leaf_ok.append(cap)
                else:
                    misses.add(cap)
            if not leaf_ok:
                continue
            key = tuple(sorted(set(leaf_ok)))
            result = br_fp_cache.get(key)
            if result is None:
                result = solver.resolve(key)
                br_fp_cache[key] = result
            if not result.ok:
                misses.update(result.missing_provides or key)
    else:
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
                # leaf holes only. pathwalk of every installable BR fans out into
                # the whole distro (perl/texlive/ghc...). dnf force covers that.
                if not repo.satisfies_requirement(cap):
                    misses.add(cap)

    for miss in misses:
        out.update(
            _distgit_fetch_candidates_for_br(
                miss,
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
    solver: Any | None = None,
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
                solver=solver,
            )
            if active_scope is not None:
                candidates |= {
                    p
                    for p in active_scope
                    if p and is_distgit_source_name(p) and p not in parsed_by_source
                }
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
            logger.info(
                "builddep expand wave %s %s/%s: candidates=%s to_fetch=%s scope=%s parsed=%s",
                wave_num,
                releasename,
                distgit_branch,
                len(candidates),
                len(to_fetch),
                len(active_scope or ()),
                len(parsed_by_source),
            )
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
            # do NOT grow active_scope to every fetched provider. that walks their
            # BRs and sucks in the whole distro. forced packages reenter via scope.

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
    solver: Any | None = None,
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
                solver=solver,
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
                packages=to_fetch[: _chain_distgit_parse_wave_size()],
            )
            if not batch_specs:
                break
            fetched_total += len(batch_specs)
            pool = _distgit_spec_parse_executor()
            futures = {
                pool.submit(parse_fn, text, f"{pkg}.spec"): pkg
                for pkg, text in batch_specs.items()
            }
            for fut in as_completed(futures):
                pkg = futures[fut]
                text = batch_specs[pkg]
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
