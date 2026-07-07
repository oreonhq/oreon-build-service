from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import tempfile
import tarfile
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from specfile import Specfile

from oreon_build.config import get_settings
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
    epoch: int = 0
    version: str = ""
    release: str = ""

    def __post_init__(self) -> None:
        if self.build_requires_raw is None:
            self.build_requires_raw = list(self.build_requires)

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


def _add_capability_tokens(target: set[str], raw: str, macros: dict[str, str]) -> None:
    expanded = _expand_rpm_macros(raw, macros).strip()
    if not expanded or "%" in expanded:
        return
    for tok in _extract_br_tokens_from_tag_value(expanded):
        if tok and "%" not in tok:
            target.add(tok)


def _extract_br_tokens_from_tag_value(value: str) -> list[str]:
    out: list[str] = []
    for chunk in (value or "").replace("\n", ",").split(","):
        token = chunk.strip()
        if not token:
            continue
        if "=" in token:
            token = token.split("=", 1)[0].strip()
        if not token:
            continue
        m = _BR_NAME.match(token)
        if not m:
            continue
        name = m.group(1)
        if name.startswith("rpmlib(") or name.startswith("/") or name.startswith("config("):
            continue
        out.append(name)
    return out


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


def _parse_spec_text(spec_text: str, filename_hint: str = "inline.spec") -> _SpecParseResult:
    br_out: list[str] = []
    br_raw: list[str] = []
    br_seen: set[str] = set()
    provided: set[str] = set()
    source_name = Path(filename_hint).stem
    epoch = 0
    version = ""
    release = ""
    macros = _parse_spec_macro_table(spec_text)
    for line in (spec_text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        us = s.upper()
        if us.startswith("NAME:"):
            val = _expand_rpm_macros(s.split(":", 1)[1].strip(), macros)
            if val and "%" not in val:
                source_name = val
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
        if us.startswith("BUILDREQUIRES:"):
            val = s.split(":", 1)[1]
            expanded = _expand_rpm_macros(val, macros)
            for chunk in expanded.replace("\n", ",").split(","):
                line = chunk.strip()
                if not line:
                    continue
                br_raw.append(line)
            for tok in _extract_br_tokens_from_tag_value(expanded):
                if tok in br_seen:
                    continue
                br_seen.add(tok)
                br_out.append(tok)
            continue
        if us.startswith("PROVIDES:"):
            val = s.split(":", 1)[1]
            for tok in _extract_br_tokens_from_tag_value(_expand_rpm_macros(val, macros)):
                if not tok or "%" in tok or tok.startswith("bundled("):
                    continue
                provided.add(tok)
    provided.add(source_name)
    if (os.environ.get("OREON_SPECFILE_DEEP_PARSE") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return _SpecParseResult(
            source_name=source_name,
            build_requires=br_out,
            provided_binary_names=provided,
            build_requires_raw=br_raw,
            epoch=epoch,
            version=version,
            release=release,
        )

    try:
        spec = Specfile(content=spec_text, force_parse=True, sourcedir=tempfile.gettempdir())
        with spec.tags() as tags:
            for t in tags:
                name = (getattr(t, "name", "") or "").strip()
                if not name:
                    continue
                lname = name.lower()
                if lname == "name" and not source_name:
                    source_name = str(getattr(t, "value", "") or "").strip()
                if lname == "provides":
                    for tok in _extract_br_tokens_from_tag_value(str(getattr(t, "value", "") or "")):
                        if not tok or "%" in tok or tok.startswith("bundled("):
                            continue
                        provided.add(tok)
                    continue
                if lname != "buildrequires":
                    continue
                for tok in _extract_br_tokens_from_tag_value(str(getattr(t, "value", "") or "")):
                    if tok in br_seen:
                        continue
                    br_seen.add(tok)
                    br_out.append(tok)
        if not source_name:
            source_name = Path(filename_hint).stem
        provided.add(source_name)
        with spec.sections() as sections:
            for sec in sections:
                if getattr(sec, "normalized_name", "") != "package":
                    continue
                pkg_name = _extract_pkg_name_from_package_options(str(getattr(sec, "options", "") or ""), source_name)
                if pkg_name:
                    provided.add(pkg_name)
        return _SpecParseResult(
            source_name=source_name,
            build_requires=br_out,
            provided_binary_names=provided,
            build_requires_raw=br_raw,
            epoch=epoch,
            version=version,
            release=release,
        )
    except Exception as exc:
        logger.warning("specfile parse failed for %s: %s", filename_hint, exc)
    return _SpecParseResult(
        source_name=source_name,
        build_requires=br_out,
        provided_binary_names=provided,
        build_requires_raw=br_raw,
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
    return provider_map


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


async def _parse_specs_parallel_async(
    specs_by_pkg: dict[str, str],
) -> tuple[dict[str, _SpecParseResult], list[str]]:
    err: list[str] = []
    parsed_by_source: dict[str, _SpecParseResult] = {}
    if not specs_by_pkg:
        return parsed_by_source, err
    loop = asyncio.get_running_loop()
    pool = _distgit_spec_parse_executor()
    items = list(specs_by_pkg.items())
    tasks = [
        loop.run_in_executor(pool, _parse_spec_text, spec_text, f"{pkg}.spec")
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
        for br in parsed.build_requires:
            providers = provider_map.get(br) or set()
            for prov in sorted(providers):
                if prov not in names_set or prov == cons:
                    continue
                deps[cons].add(prov)
                rdeps[prov].add(cons)
                edges.append(
                    {
                        "from": cons,
                        "to": prov,
                        "requirement": br,
                        "satisfied": False,
                        "type": "distgit_br",
                    }
                )

    layers, cyc = _kahn_layers(names_set, deps, rdeps)
    if cyc:
        err.append("Warning: a cycle exists in distgit BuildRequires; some parallel layers may be conservative.")

    capability_providers = {cap: sorted(srcs) for cap, srcs in sorted(provider_map.items())}
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
    provider_map: dict[str, set[str]] = {}
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
            pool.submit(_parse_spec_text, batch_specs[pkg], f"{pkg}.spec"): pkg
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
                provider_map.setdefault(cap, set()).update(srcs)
            for br in parsed.build_requires:
                provs = set(provider_map.get(br) or set())
                if br in names_set:
                    provs.add(br)
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
    t_closure = time.monotonic()
    specs_by_pkg, parsed_during_closure, cerr = await asyncio.to_thread(
        _expand_chain_spec_closure,
        root_packages,
        all_names,
        tarball,
        releasename,
        members=members,
    )
    logger.info(
        "distgit BR closure: %s specs from %s root(s) in %.1fs",
        len(specs_by_pkg),
        len(root_packages),
        time.monotonic() - t_closure,
    )
    err.extend(cerr)
    if parsed_during_closure and len(parsed_during_closure) >= len(specs_by_pkg):
        parsed_by_source = parsed_during_closure
        perr = []
    else:
        parsed_by_source, perr = await _parse_specs_parallel_async(specs_by_pkg)
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

    parsed_by_source, perr = await _parse_specs_parallel_async(specs_by_pkg)
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
    tasks = [
        loop.run_in_executor(pool, _parse_spec_text, spec_text, f"{pkg}.spec")
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
        for br in parsed.build_requires:
            providers = provider_map.get(br) or set()
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
