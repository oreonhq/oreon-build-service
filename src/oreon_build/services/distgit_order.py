"""Topological build order and spec-tree resolver from distgit spec files (GitHub)."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import tempfile
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from specfile import Specfile

from oreon_build.config import get_settings
from oreon_build.services.r2 import get_r2_client

logger = logging.getLogger(__name__)

# Match BuildRequires name token (source-package-like token extraction)
_BR_NAME = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9+._-]*)")


@dataclass
class DistgitBuildOrderResult:
    releasename: str
    distgit_url: str
    distgit_branch: str
    package_names: list[str]
    edges: list[dict[str, str]]  # { "from": consumer, "to": provider } consumer needs provider
    layers: list[list[str]]  # topological levels (parallel groups)
    # Capability name (Provides / subpackage / source name) -> distgit source packages that provide it.
    capability_providers: dict[str, list[str]]
    errors: list[str]
    cycle_warning: bool


@dataclass
class SpecTreeResolveResult:
    releasename: str
    repo_branch: str
    distgit_url: str
    distgit_branch: str
    mode: str  # "all_missing" | "existing_only"
    total_spec_packages: int
    built_sources: list[str]
    never_built_sources: list[str]
    selected_for_chain: list[str]
    required_missing_sources: list[str]
    edges: list[dict[str, str]]  # selected consumer -> selected provider
    layers: list[list[str]]
    chain_string: str
    unresolved_requirements: list[dict[str, str]]  # consumer + requirement for provider not in spec tree
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


def _extract_br_tokens_from_tag_value(value: str) -> list[str]:
    out: list[str] = []
    for chunk in (value or "").replace("\n", ",").split(","):
        token = chunk.strip()
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
    """
    Parse `%package` options and return the emitted binary package name.
    Supports both:
    - `%package devel` -> `<source>-devel`
    - `%package -n explicit-name` -> `explicit-name`
    """
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
        # Other options we do not model here.
        return None
    return f"{source_name}-{suffix}"


def _parse_spec_text(spec_text: str, filename_hint: str = "inline.spec") -> _SpecParseResult:
    br_out: list[str] = []
    br_seen: set[str] = set()
    provided: set[str] = set()
    source_name = Path(filename_hint).stem
    # Fast path parser (line oriented) for large trees.
    # This avoids `specfile`'s expensive parse on thousands of specs.
    for line in (spec_text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        us = s.upper()
        if us.startswith("NAME:"):
            val = s.split(":", 1)[1].strip()
            if val:
                source_name = val
            continue
        if us.startswith("%PACKAGE "):
            opts = s.split(None, 1)[1].strip() if len(s.split(None, 1)) > 1 else ""
            pkg_name = _extract_pkg_name_from_package_options(opts, source_name)
            if pkg_name:
                provided.add(pkg_name)
            continue
        if us.startswith("BUILDREQUIRES:"):
            val = s.split(":", 1)[1]
            for tok in _extract_br_tokens_from_tag_value(val):
                if tok in br_seen:
                    continue
                br_seen.add(tok)
                br_out.append(tok)
    provided.add(source_name)
    # Optional slow path for max fidelity. Keep disabled by default.
    if (os.environ.get("OREON_SPECFILE_DEEP_PARSE") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return _SpecParseResult(source_name=source_name, build_requires=br_out, provided_binary_names=provided)

    # Slow path with specfile library (opt-in).
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
        return _SpecParseResult(source_name=source_name, build_requires=br_out, provided_binary_names=provided)
    except Exception as exc:
        logger.warning("specfile parse failed for %s: %s", filename_hint, exc)
    # Return the fast-path result if deep parse fails.
    return _SpecParseResult(source_name=source_name, build_requires=br_out, provided_binary_names=provided)


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
    """deps[a] = predecessor packages a needs built first. Kahn; on cycle, peel one node per round."""
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


async def _fetch_specs_by_tarball(
    *,
    owner: str,
    repo: str,
    releasename: str,
    distgit_branch: str,
    headers: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Download branch tarball once, extract `<releasename>/<pkg>/<pkg>.spec` entries."""
    errs: list[str] = []
    u = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{distgit_branch}"
    async with httpx.AsyncClient(timeout=180, headers=headers, follow_redirects=True) as client:
        r = await client.get(u)
    if r.status_code != 200:
        return {}, [f"Tarball fetch failed HTTP {r.status_code}: {u}"]
    specs: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                parts = m.name.split("/")
                if len(parts) < 4:
                    continue
                # <repo>-<branch>/<releasename>/<pkg>/<pkg>.spec
                if parts[1] != releasename:
                    continue
                pkg = parts[2]
                fn = parts[3]
                if fn != f"{pkg}.spec":
                    continue
                f = tf.extractfile(m)
                if f is None:
                    continue
                specs[pkg] = f.read().decode("utf-8", errors="replace")
    except Exception as exc:
        errs.append(f"tarball parse failed: {type(exc).__name__}: {exc}")
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
    """List packages under ``releasename/`` in distgit, parse specs, return topo order."""
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

    names_set: set[str] = set(specs_by_pkg.keys())
    parsed_by_source: dict[str, _SpecParseResult] = {}
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryDirectory(prefix="oreon-distgit-") as _tmp:
        tasks = [
            loop.run_in_executor(None, _parse_spec_text, spec_text, f"{pkg}.spec")
            for pkg, spec_text in specs_by_pkg.items()
        ]
        parse_results = await asyncio.gather(*tasks, return_exceptions=True)
    for (pkg, _spec_text), parsed_or_exc in zip(specs_by_pkg.items(), parse_results, strict=False):
        if isinstance(parsed_or_exc, Exception):
            err.append(f"{pkg}: parse failed ({type(parsed_or_exc).__name__}: {parsed_or_exc})")
            parsed_by_source[pkg] = _SpecParseResult(source_name=pkg, build_requires=[], provided_binary_names={pkg})
        else:
            parsed_by_source[pkg] = parsed_or_exc

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
                # cons depends on provider source package
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

    # Transitive trim for layer sort only
    layers, cyc = _kahn_layers(names_set, deps, rdeps)
    if cyc:
        err.append("Warning: a cycle exists in distgit BuildRequires; some parallel layers may be conservative.")

    capability_providers = {cap: sorted(srcs) for cap, srcs in sorted(provider_map.items())}

    return DistgitBuildOrderResult(
        releasename=releasename,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        package_names=sorted(names_set),
        edges=edges,
        layers=layers,
        capability_providers=capability_providers,
        errors=err,
        cycle_warning=cyc,
    )


def resolve_distgit_sources_for_capability(
    order: DistgitBuildOrderResult,
    capability: str,
) -> list[str]:
    """Map a dnf/RPM capability (Provides token or package name) to distgit source name(s)."""
    cap = (capability or "").strip()
    if not cap:
        return []
    spec_set = set(order.package_names)
    if cap in spec_set:
        return [cap]
    prov_map = order.capability_providers or {}
    hit = prov_map.get(cap)
    if hit:
        return list(hit)
    low = cap.lower()
    for key, srcs in prov_map.items():
        if key.lower() == low:
            return list(srcs)
    return []


async def compute_spec_tree_dependency_plan(
    *,
    releasename: str,
    repo_branch: str = "dev",
    existing_only: bool = False,
    distgit_url: str = "https://github.com/oreonhq/rpm-specfiles.git",
    distgit_branch: str = "main",
) -> SpecTreeResolveResult:
    """
    Resolve a chain plan from spec-tree + R2 source RPMs only.

    - Parse ALL specs from `<releasename>/` in distgit (parallel).
    - Compare against built SOURCE packages under `R2 <releasename>/<repo_branch>/src/*.src.rpm`.
    - No DB package-state comparison.
    """
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
    tasks = [
        loop.run_in_executor(None, _parse_spec_text, spec_text, f"{pkg}.spec")
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
    """Return all dist-git packages under release that are not present in DB."""
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
