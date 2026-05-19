"""Dependency graph built from BuildRequires in source RPMs."""
from __future__ import annotations

import gzip
import bz2
import hashlib
import lzma
import asyncio
import logging
import os
import re
import time
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import Release, ReleaseRepo, Repository
from oreon_build.services.r2 import get_r2_client

logger = logging.getLogger(__name__)

# In-process response cache so the dependencies UI does not hammer R2 and rpm on every refresh.
_deps_graph_cache_at: float = 0.0
_deps_graph_cache_payload: dict[str, Any] | None = None

RPM_NS = "{http://linux.duke.edu/metadata/rpm}"
COMMON_NS = "{http://linux.duke.edu/metadata/common}"

# Persistent cache for extracted BuildRequires so repeated API calls are fast.
# Priority: /var/cache (system) -> ~/.cache (user) -> /tmp (fallback).
def _init_br_cache_dir() -> Path:
    for candidate in [
        Path("/var/cache/oreon-srpm-br-cache"),
        Path.home() / ".cache" / "oreon-srpm-br-cache",
        Path(tempfile.gettempdir()) / "oreon-srpm-br-cache",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            # Quick write test to ensure we have permission.
            (candidate / ".probe").write_text("ok")
            (candidate / ".probe").unlink()
            return candidate
        except OSError:
            continue
    raise RuntimeError("Cannot create SRPM BR cache directory")

_BR_CACHE_DIR = _init_br_cache_dir()


@dataclass(frozen=True)
class RepoTarget:
    releasename: str
    branch: str
    arch: str
    prefix: str

    @property
    def key(self) -> str:
        return f"{self.releasename}|{self.branch}|{self.arch}"


_SIMPLE_PKG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*$")


def _safe_text(node: ET.Element | None, default: str = "") -> str:
    if node is None or node.text is None:
        return default
    return node.text.strip()


def _source_name_from_srpm_filename(raw: str) -> str | None:
    s = (raw or "").strip()
    if s.endswith(".src.rpm"):
        s = s[:-8]
    parts = s.rsplit("-", 2)
    if len(parts) != 3:
        return None
    return parts[0] or None


def _source_name_from_sourcerpm(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s.endswith(".src.rpm"):
        return None
    base = s[:-8]
    parts = base.rsplit("-", 2)
    if len(parts) != 3:
        return None
    return parts[0] or None


def _decompress_if_needed(raw: bytes, key: str) -> bytes:
    k = key.lower()
    if k.endswith(".gz"):
        return gzip.decompress(raw)
    if k.endswith(".bz2"):
        return bz2.decompress(raw)
    if k.endswith(".xz"):
        return lzma.decompress(raw)
    if k.endswith(".zst") or k.endswith(".zstd"):
        try:
            import zstandard as zstd  # type: ignore
            return zstd.ZstdDecompressor().decompress(raw)
        except Exception:
            p = subprocess.run(
                ["zstd", "-d", "-c", "--no-progress"],
                input=raw,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if p.returncode == 0 and p.stdout:
                return p.stdout
            raise RuntimeError("Failed to decompress zstd metadata")
    return raw


def _resolve_repodata_key(repo_prefix: str, repomd_bytes: bytes, data_type: str) -> str | None:
    try:
        root = ET.fromstring(repomd_bytes)
    except ET.ParseError:
        return None
    for data in root.findall(".//{*}data"):
        if (data.attrib.get("type") or "").strip() != data_type:
            continue
        loc = data.find("{*}location")
        href = (loc.attrib.get("href") if loc is not None else "") or ""
        href = href.strip()
        if not href:
            continue
        if href.startswith(f"{repo_prefix}/"):
            return href
        return f"{repo_prefix}/{href.lstrip('/')}"
    return None


def _resolve_primary_key(repo_prefix: str, repomd_bytes: bytes) -> str | None:
    return _resolve_repodata_key(repo_prefix, repomd_bytes, "primary")


def _evr_from_primary_version(pkg: ET.Element) -> Any:
    from oreon_build.services.chain_satisfaction import RpmEvr

    ver_el = pkg.find("{*}version")
    if ver_el is None:
        return RpmEvr(0, "0", "0")
    try:
        epoch = int((ver_el.attrib.get("epoch") or "0").strip() or "0")
    except ValueError:
        epoch = 0
    version = (ver_el.attrib.get("ver") or "0").strip() or "0"
    release = (ver_el.attrib.get("rel") or "0").strip() or "0"
    return RpmEvr(epoch, version, release)


def _merge_best_evr(current: Any | None, candidate: Any) -> Any:
    from oreon_build.services.chain_satisfaction import _vercmp

    if current is None:
        return candidate
    return candidate if _vercmp(candidate, current) > 0 else current


def load_repo_satisfaction_index(
    target: RepoTarget,
) -> tuple[Any, str | None]:
    """
    Parse primary.xml into provides, known sources, and best EVR per provide/source.

    Returns ``(RepoSatisfactionIndex, error)``.
    """
    from oreon_build.services.chain_satisfaction import RepoSatisfactionIndex

    r2 = get_r2_client()
    repomd_key = f"{target.prefix}/repodata/repomd.xml"
    repomd = r2.get_object(repomd_key)
    if not repomd:
        return RepoSatisfactionIndex({}, set(), {}, {}), "repomd_missing"
    primary_key = _resolve_primary_key(target.prefix, repomd)
    if not primary_key:
        return RepoSatisfactionIndex({}, set(), {}, {}), "primary_missing_in_repomd"
    primary_raw = r2.get_object(primary_key)
    if not primary_raw:
        return RepoSatisfactionIndex({}, set(), {}, {}), "primary_blob_missing"
    try:
        primary_xml = _decompress_if_needed(primary_raw, primary_key)
        provides_index, known_sources = _build_provides_index(primary_xml)
        provide_best: dict[str, Any] = {}
        source_best: dict[str, Any] = {}
        root = ET.fromstring(primary_xml)
        for pkg in root.findall(".//{*}package"):
            arch = _safe_text(pkg.find("{*}arch"))
            if arch == "src":
                continue
            src_name = _source_name_from_sourcerpm(_safe_text(pkg.find("{*}format/{*}sourcerpm")))
            if not src_name:
                src_name = _safe_text(pkg.find("{*}name"))
            if not src_name:
                continue
            evr = _evr_from_primary_version(pkg)
            source_best[src_name] = _merge_best_evr(source_best.get(src_name), evr)
            bin_name = _safe_text(pkg.find("{*}name"))
            if bin_name:
                provide_best[bin_name] = _merge_best_evr(provide_best.get(bin_name), evr)
            for e in pkg.findall(f".//{RPM_NS}provides/{RPM_NS}entry"):
                pname = (e.attrib.get("name") or "").strip()
                if pname:
                    provide_best[pname] = _merge_best_evr(provide_best.get(pname), evr)
        return (
            RepoSatisfactionIndex(
                provides_index=provides_index,
                known_sources=known_sources,
                provide_best_evr=provide_best,
                source_best_evr=source_best,
            ),
            None,
        )
    except Exception as exc:
        return RepoSatisfactionIndex({}, set(), {}, {}), f"primary_parse_failed:{type(exc).__name__}:{exc}"


def load_repo_provides_index(
    target: RepoTarget,
) -> tuple[dict[str, set[str]], set[str], str | None]:
    """
    Parse published repo primary.xml into provide_name -> source package names.

    Returns ``(provides_index, known_built_sources, error)`` where ``error`` is set on failure.
    """
    r2 = get_r2_client()
    repomd_key = f"{target.prefix}/repodata/repomd.xml"
    repomd = r2.get_object(repomd_key)
    if not repomd:
        return {}, set(), "repomd_missing"
    primary_key = _resolve_primary_key(target.prefix, repomd)
    if not primary_key:
        return {}, set(), "primary_missing_in_repomd"
    primary_raw = r2.get_object(primary_key)
    if not primary_raw:
        return {}, set(), "primary_blob_missing"
    try:
        primary_xml = _decompress_if_needed(primary_raw, primary_key)
        pi, known = _build_provides_index(primary_xml)
        return pi, known, None
    except Exception as exc:
        return {}, set(), f"primary_parse_failed:{type(exc).__name__}:{exc}"


def _resolve_primary_href(repomd_bytes: bytes) -> str | None:
    try:
        root = ET.fromstring(repomd_bytes)
    except ET.ParseError:
        return None
    for data in root.findall(".//{*}data"):
        if (data.attrib.get("type") or "").strip() != "primary":
            continue
        loc = data.find("{*}location")
        href = (loc.attrib.get("href") if loc is not None else "") or ""
        href = href.strip()
        if href:
            return href
    return None


def _build_provides_index(
    primary_xml: bytes,
) -> tuple[dict[str, set[str]], set[str]]:
    """Parse primary.xml to build:
    - provides_index: provide_name -> set of source package names that provide it
    - known_sources: all source package names known from primary.xml
    """
    provides_index: dict[str, set[str]] = {}
    known_sources: set[str] = set()

    try:
        root = ET.fromstring(primary_xml)
    except ET.ParseError as exc:
        logger.warning("primary.xml parse error: %s", exc)
        return provides_index, known_sources

    for pkg in root.findall(".//{*}package"):
        bin_name = _safe_text(pkg.find("{*}name"))
        if not bin_name:
            continue
        arch = _safe_text(pkg.find("{*}arch"))
        if arch == "src":
            continue

        # Determine source package name from sourcerpm field (authoritative).
        src_name = _source_name_from_sourcerpm(_safe_text(pkg.find("{*}format/{*}sourcerpm")))
        if not src_name:
            # Fallback: binary name is the source name (top-level package).
            src_name = bin_name

        known_sources.add(src_name)

        # Index every explicit provide.
        for e in pkg.findall(f".//{RPM_NS}provides/{RPM_NS}entry"):
            pname = (e.attrib.get("name") or "").strip()
            if pname:
                provides_index.setdefault(pname, set()).add(src_name)

        # Every binary package implicitly provides its own name.
        provides_index.setdefault(bin_name, set()).add(src_name)

    return provides_index, known_sources


def _parse_br_tokens(raw: str) -> list[str]:
    """Extract clean package name tokens from a BuildRequires line.

    Handles:
    - Simple names:           ``make``
    - Versioned:              ``glibc-devel >= 2.34``
    - Rich deps:              ``(python3dist(tomli) if python3-devel < 3.11)``
    - Perl/Python dist deps:  ``python3dist(requests)``
    - File paths:             ``/usr/bin/perl`` (skipped)
    - rpmlib tokens:          ``rpmlib(CompressedFileNames) <= 3.0.4-1`` (skipped)
    """
    tokens: list[str] = []
    # Strip outer parens from rich-dep expressions.
    s = raw.strip()
    # Remove surrounding parentheses for rich-dep groups.
    while s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()

    # Split on whitespace + boolean operators to get individual name/version tokens.
    parts = re.split(r"\s+", s)
    skip_next = False
    for p in parts:
        if skip_next:
            skip_next = False
            continue
        # Skip boolean keywords.
        if p.lower() in {"and", "or", "if", "else", "with", "without", "unless"}:
            continue
        # Skip comparison operators and the version token that follows.
        if re.fullmatch(r"[<>=!]+", p):
            skip_next = True
            continue
        # Skip pure version strings.
        if re.fullmatch(r"[0-9][0-9a-zA-Z._+~^-]*", p):
            continue
        # Skip rpmlib, config, file deps.
        p_stripped = p.strip("()")
        if (
            p_stripped.startswith("rpmlib(")
            or p_stripped.startswith("config(")
            or p_stripped.startswith("/")
        ):
            continue
        # python3dist(foo) / perl(Bar::Baz) style: extract the inner name too.
        m = re.match(r"([A-Za-z0-9_][A-Za-z0-9_+.-]*dist)\(([^)]+)\)", p_stripped)
        if m:
            # Keep the outer dist-style provide (e.g. "python3dist(requests)") as-is
            # so the provides_index can match it if something in the repo provides it.
            tokens.append(p_stripped.rstrip(")").lstrip("(") )
            continue
        # Regular package name token.
        clean = p_stripped.strip("()")
        if clean and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_+.:@-]*", clean):
            tokens.append(clean)

    return tokens


def _parse_runtime_req_name(raw: str) -> str | None:
    """Normalize one runtime Requires name from primary.xml.

    Keep capability names as-is so they can be matched against provides index.
    Skip file/rpmlib/config deps because they are not source-package edges.
    """
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("/") or s.startswith("rpmlib(") or s.startswith("config("):
        return None
    return s


def _get_buildrequires_cached(r2_client: Any, srpm_key: str) -> list[str]:
    """Download an SRPM from R2 and extract its BuildRequires using rpm(1).

    Results are cached to disk so repeated calls (same SRPM key) are instant.
    """
    cache_key = hashlib.sha256(srpm_key.encode()).hexdigest()
    cache_file = _BR_CACHE_DIR / f"{cache_key}.br"

    if cache_file.exists():
        text = cache_file.read_text("utf-8").strip()
        return text.splitlines() if text else []

    data = r2_client.get_object(srpm_key)
    if not data:
        return []

    with tempfile.NamedTemporaryFile(suffix=".src.rpm", delete=False) as f:
        f.write(data)
        srpm_path = f.name

    try:
        result = subprocess.run(
            ["rpm", "--nosignature", "-qp", "--requires", srpm_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        brs: list[str] = []
        for line in result.stdout.splitlines():
            tokens = _parse_br_tokens(line.strip())
            brs.extend(tokens)
        # Deduplicate while preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for t in brs:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        cache_file.write_text("\n".join(unique), "utf-8")
        return unique
    finally:
        try:
            os.unlink(srpm_path)
        except OSError:
            pass


def _map_br_to_source(
    br: str,
    provides_index: dict[str, set[str]],
    src_candidates: set[str],
    known_built_sources: set[str],
) -> set[str] | None:
    """Map a BuildRequire name to the set of source packages that provide it.

    Returns:
    - A non-empty set of source names if the BR is satisfied by the repo.
    - None if the BR is not satisfied and cannot be attributed to any Oreon source.
    - An empty set (falsy but not None) should not occur; treat missing as None.

    Strategy (no hardcoding, no arbitrary filters):
    1. If the BR name is literally in provides_index -> satisfied, return providers.
    2. If the BR name exactly matches a source candidate -> unbuilt Oreon package.
    3. If the BR name prefix-matches a source candidate that has NOT yet produced
       any binary in the repo (i.e., the source is in src_candidates but NOT in
       known_built_sources) -> attribute to that unbuilt source.
    4. Otherwise -> from base repos or unknown; skip (return None).
    """
    # Step 1: direct provides lookup.
    providers = provides_index.get(br)
    if providers:
        return set(providers)

    # Step 2: exact match to a source package name in our src/ index.
    if br in src_candidates:
        return {br}

    # Step 3: conservative prefix match, but ONLY for sources that haven't been
    # built yet. This handles cases where package `foo` produces `foo-devel` which
    # is a BuildRequire of something, but `foo-devel` isn't yet in the repo.
    # We do NOT prefix-match against sources that are already built (to avoid
    # attributing base-repo packages like `perl-Archive-Tar` to source `perl`).
    prefix_matches = [
        s for s in src_candidates
        if br.startswith(s + "-") and s not in known_built_sources
    ]
    if prefix_matches:
        # Longest prefix wins.
        best = max(prefix_matches, key=len)
        return {best}

    # Step 4: built source that owns a subpackage-style capability name but repodata lacks it
    # (e.g. old build without a Provides: alias consumers still Require).
    if br not in provides_index:
        built_prefix = [
            s for s in src_candidates
            if s in known_built_sources and (br == s or br.startswith(s + "-"))
        ]
        if built_prefix:
            return {max(built_prefix, key=len)}

    # Step 5: not our package.
    return None


def _providers_from_spec_capability_map(
    requirement: str,
    spec_capability_providers: dict[str, list[str]] | None,
) -> set[str] | None:
    """Map a capability to distgit source package(s) from parsed spec Provides metadata."""
    if not spec_capability_providers:
        return None
    cap = (requirement or "").strip()
    if not cap:
        return None
    for key in (cap, cap.lower()):
        hit = spec_capability_providers.get(key)
        if hit:
            return {str(s) for s in hit if s}
    cap_low = cap.lower()
    for key, srcs in spec_capability_providers.items():
        if key.lower() == cap_low and srcs:
            return {str(s) for s in srcs if s}
    return None


def _providers_for_requirement(
    requirement: str,
    provides_index: dict[str, set[str]],
    src_candidates: set[str],
    known_built_sources: set[str],
    spec_capability_providers: dict[str, list[str]] | None = None,
) -> set[str] | None:
    """
    Resolve who provides a BuildRequires or runtime Requires name.

    Uses published-repo src/ layout first, then distgit spec Provides (packages may
    exist in specs but have never been built to R2 yet).
    """
    providers = _map_br_to_source(requirement, provides_index, src_candidates, known_built_sources)
    if providers is not None:
        return providers
    return _providers_from_spec_capability_map(requirement, spec_capability_providers)


def _capability_listed_in_provides_index(capability: str, provides_index: dict[str, set[str]]) -> bool:
    cap = (capability or "").strip()
    if not cap:
        return True
    if provides_index.get(cap):
        return True
    cap_low = cap.lower()
    for key in provides_index:
        if key.lower() == cap_low and provides_index[key]:
            return True
    return False


def _requirement_satisfied_in_repo(
    requirement: str,
    providers: set[str],
    known_built_sources: set[str],
    provides_index: dict[str, set[str]],
) -> bool:
    """Built providers are not enough; the requirement name must appear in repodata Provides."""
    if not providers:
        return True
    if not all(p in known_built_sources for p in providers):
        return False
    return _capability_listed_in_provides_index(requirement, provides_index)


def _collect_srpm_keys(r2_client: Any, src_prefix: str) -> dict[str, str]:
    """Return {source_name: best_srpm_r2_key} for a src/ prefix.

    When multiple SRPMs exist for the same source (e.g. .fc45. and .or11.),
    prefer the Oreon-tagged one (.or11., .or12., etc.) over upstream snapshots.
    """
    srpm_keys: dict[str, str] = {}
    for k in r2_client.iter_keys_with_prefix(src_prefix):
        if not k.endswith(".src.rpm"):
            continue
        fn = k.rsplit("/", 1)[-1]
        src_name = _source_name_from_srpm_filename(fn)
        if not src_name:
            continue
        existing = srpm_keys.get(src_name)
        if existing is None:
            srpm_keys[src_name] = k
        else:
            # Prefer Oreon-tagged builds over fc/rawhide snapshots.
            has_oreon_tag = re.search(r"\.or\d+\.", fn) is not None
            existing_has_oreon = re.search(r"\.or\d+\.", existing.rsplit("/", 1)[-1]) is not None
            if has_oreon_tag and not existing_has_oreon:
                srpm_keys[src_name] = k
    return srpm_keys


def _analyze_repo(
    target: RepoTarget,
    external_provides: set[str] | None = None,
    spec_capability_providers: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    r2 = get_r2_client()

    # 1. Fetch and parse repomd + primary.xml to build the provides index.
    _empty = {
        "repo": target,
        "packages": {},
        "missing": [],
        "edges": [],
        "runtime_missing": [],
        "runtime_edges": [],
    }

    repomd_key = f"{target.prefix}/repodata/repomd.xml"
    repomd = r2.get_object(repomd_key)
    if not repomd:
        return {**_empty, "error": "repomd_missing"}

    primary_key = _resolve_primary_key(target.prefix, repomd)
    if not primary_key:
        return {**_empty, "error": "primary_missing_in_repomd"}

    primary_raw = r2.get_object(primary_key)
    if not primary_raw:
        return {**_empty, "error": "primary_blob_missing"}

    try:
        primary_xml = _decompress_if_needed(primary_raw, primary_key)
        provides_index, known_built_sources = _build_provides_index(primary_xml)
    except Exception as exc:
        return {
            "repo": target,
            "packages": {},
            "missing": [],
            "edges": [],
            "runtime_missing": [],
            "runtime_edges": [],
            "error": f"primary_parse_failed:{type(exc).__name__}:{exc}",
        }

    # 2. List SRPMs from the src/ directory for this release+branch.
    src_prefix = f"{target.releasename}/{target.branch}/src/"
    srpm_keys = _collect_srpm_keys(r2, src_prefix)
    src_candidates = set(srpm_keys.keys())

    if not src_candidates:
        return {
            "repo": target,
            "packages": {},
            "missing": [],
            "edges": [],
            "runtime_missing": [],
            "runtime_edges": [],
            "error": None,
        }

    # 3. Fetch BuildRequires for all SRPMs in parallel (cached on disk).
    srpm_brs: dict[str, list[str]] = {}
    raw_workers = (os.getenv("OREON_CHAIN_REPO_ANALYZE_WORKERS") or "24").strip()
    try:
        analyze_workers = int(raw_workers)
    except ValueError:
        analyze_workers = 24
    analyze_workers = max(4, min(analyze_workers, 64))
    with ThreadPoolExecutor(max_workers=analyze_workers) as pool:
        future_to_name = {
            pool.submit(_get_buildrequires_cached, r2, key): src_name
            for src_name, key in srpm_keys.items()
        }
        for future in as_completed(future_to_name):
            src_name = future_to_name[future]
            try:
                srpm_brs[src_name] = future.result()
            except Exception as exc:
                logger.warning("BR extraction failed for %s: %s", src_name, exc)
                srpm_brs[src_name] = []

    # 4. Build edges and missing entries from BuildRequires.
    edges: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    runtime_edges: list[dict[str, Any]] = []
    runtime_missing: list[dict[str, Any]] = []
    packages: dict[str, dict[str, Any]] = {}

    for src_name, brs in srpm_brs.items():
        packages[src_name] = {
            "name": src_name,
            "provides": sorted(provides_index.get(src_name, set())),
            "build_requires": sorted(set(brs)),
        }
        seen_req: set[str] = set()
        for br in brs:
            if br in seen_req:
                continue
            seen_req.add(br)

            # Skip if satisfied by external (base) repos.
            if external_provides and br in external_provides:
                continue

            providers = _providers_for_requirement(
                br,
                provides_index,
                src_candidates,
                known_built_sources,
                spec_capability_providers,
            )

            if providers is None:
                # Not attributable to any Oreon source (base repos, unknown). Skip.
                continue

            is_satisfied = _requirement_satisfied_in_repo(
                br, providers, known_built_sources, provides_index
            )

            for prov_src in sorted(providers):
                if prov_src == src_name:
                    continue
                edges.append(
                    {
                        "release": target.releasename,
                        "branch": target.branch,
                        "arch": target.arch,
                        "from": src_name,
                        "to": prov_src,
                        "requirement": br,
                        "missing": not is_satisfied,
                    }
                )
            if not is_satisfied:
                # The required package exists in src/ but hasn't been built yet
                # (or is built but doesn't produce the required binary yet).
                for prov_src in sorted(providers):
                    if prov_src == src_name:
                        continue
                    missing.append(
                        {
                            "release": target.releasename,
                            "branch": target.branch,
                            "arch": target.arch,
                            "package": src_name,
                            "requirement": br,
                            "provider": prov_src,
                        }
                    )

    # 5. Build runtime (install-time) Requires edges from primary.xml.
    #    We map binary package requires back to source package edges.
    try:
        root = ET.fromstring(primary_xml)
    except ET.ParseError:
        root = None
    if root is not None:
        for pkg in root.findall(".//{*}package"):
            pkg_arch = _safe_text(pkg.find("{*}arch"))
            if pkg_arch == "src":
                continue
            src_name = _source_name_from_sourcerpm(_safe_text(pkg.find("{*}format/{*}sourcerpm")))
            if not src_name:
                continue

            seen_runtime_req: set[str] = set()
            for e in pkg.findall(f".//{RPM_NS}requires/{RPM_NS}entry"):
                req_raw = (e.attrib.get("name") or "").strip()
                req = _parse_runtime_req_name(req_raw)
                if not req or req in seen_runtime_req:
                    continue
                seen_runtime_req.add(req)

                # External/base repos satisfy this requirement, not an Oreon blocker.
                if external_provides and req in external_provides:
                    continue

                providers = _providers_for_requirement(
                    req,
                    provides_index,
                    src_candidates,
                    known_built_sources,
                    spec_capability_providers,
                )
                if providers is None:
                    continue
                is_satisfied = _requirement_satisfied_in_repo(
                    req, providers, known_built_sources, provides_index
                )
                for prov_src in sorted(providers):
                    if prov_src == src_name:
                        continue
                    runtime_edges.append(
                        {
                            "release": target.releasename,
                            "branch": target.branch,
                            "arch": target.arch,
                            "from": src_name,
                            "to": prov_src,
                            "requirement": req,
                            "missing": not is_satisfied,
                        }
                    )
                if not is_satisfied:
                    for prov_src in sorted(providers):
                        if prov_src == src_name:
                            continue
                        runtime_missing.append(
                            {
                                "release": target.releasename,
                                "branch": target.branch,
                                "arch": target.arch,
                                "package": src_name,
                                "requirement": req,
                                "provider": prov_src,
                            }
                        )

    return {
        "repo": target,
        "packages": packages,
        "missing": missing,
        "edges": edges,
        "runtime_missing": runtime_missing,
        "runtime_edges": runtime_edges,
        "error": None,
    }


def analyze_repo_runtime_primary_only(
    target: RepoTarget,
    *,
    external_provides: set[str] | None = None,
    spec_capability_providers: dict[str, list[str]] | None = None,
    consumer_sources: set[str] | None = None,
) -> dict[str, Any]:
    """
    Fast chain prepare path: runtime Requires from primary.xml only (no SRPM header scans).

    When ``consumer_sources`` is set, only binary packages built from those source names are scanned
    (avoids walking every RPM in a large published repo during chain prepare).
    """
    _empty: dict[str, Any] = {
        "repo": target,
        "packages": {},
        "missing": [],
        "edges": [],
        "runtime_missing": [],
        "runtime_edges": [],
    }
    r2 = get_r2_client()
    repomd_key = f"{target.prefix}/repodata/repomd.xml"
    repomd = r2.get_object(repomd_key)
    if not repomd:
        return {**_empty, "error": "repomd_missing"}
    primary_key = _resolve_primary_key(target.prefix, repomd)
    if not primary_key:
        return {**_empty, "error": "primary_missing_in_repomd"}
    primary_raw = r2.get_object(primary_key)
    if not primary_raw:
        return {**_empty, "error": "primary_blob_missing"}
    try:
        primary_xml = _decompress_if_needed(primary_raw, primary_key)
        provides_index, known_built_sources = _build_provides_index(primary_xml)
    except Exception as exc:
        return {**_empty, "error": f"primary_parse_failed:{type(exc).__name__}:{exc}"}

    src_candidates = set(known_built_sources)
    runtime_edges: list[dict[str, Any]] = []
    runtime_missing: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(primary_xml)
    except ET.ParseError:
        root = None
    if root is not None:
        for pkg in root.findall(".//{*}package"):
            pkg_arch = _safe_text(pkg.find("{*}arch"))
            if pkg_arch == "src":
                continue
            src_name = _source_name_from_sourcerpm(_safe_text(pkg.find("{*}format/{*}sourcerpm")))
            if not src_name:
                continue
            if consumer_sources is not None and src_name not in consumer_sources:
                continue
            seen_runtime_req: set[str] = set()
            for e in pkg.findall(f".//{RPM_NS}requires/{RPM_NS}entry"):
                req_raw = (e.attrib.get("name") or "").strip()
                req = _parse_runtime_req_name(req_raw)
                if not req or req in seen_runtime_req:
                    continue
                seen_runtime_req.add(req)
                if external_provides and req in external_provides:
                    continue
                providers = _providers_for_requirement(
                    req,
                    provides_index,
                    src_candidates,
                    known_built_sources,
                    spec_capability_providers,
                )
                if providers is None:
                    continue
                is_satisfied = _requirement_satisfied_in_repo(
                    req, providers, known_built_sources, provides_index
                )
                for prov_src in sorted(providers):
                    if prov_src == src_name:
                        continue
                    runtime_edges.append(
                        {
                            "release": target.releasename,
                            "branch": target.branch,
                            "arch": target.arch,
                            "from": src_name,
                            "to": prov_src,
                            "requirement": req,
                            "missing": not is_satisfied,
                        }
                    )
                if not is_satisfied:
                    for prov_src in sorted(providers):
                        if prov_src == src_name:
                            continue
                        runtime_missing.append(
                            {
                                "release": target.releasename,
                                "branch": target.branch,
                                "arch": target.arch,
                                "package": src_name,
                                "requirement": req,
                                "provider": prov_src,
                            }
                        )

    return {
        "repo": target,
        "packages": {},
        "missing": [],
        "edges": [],
        "runtime_missing": runtime_missing,
        "runtime_edges": runtime_edges,
        "error": None,
    }


async def _collect_repo_targets(db: AsyncSession) -> list[RepoTarget]:
    targets: dict[tuple[str, str, str], RepoTarget] = {}

    repo_rows = await db.execute(select(Repository))
    for repo in repo_rows.scalars().all():
        release = await db.get(Release, repo.release_id)
        if release is None:
            continue
        rel = (release.releasename or "").strip()
        ch = (repo.channel or "").strip()
        arch = (repo.architecture or "").strip()
        if not rel or not ch or not arch:
            continue
        key = (rel, ch, arch)
        targets[key] = RepoTarget(releasename=rel, branch=ch, arch=arch, prefix=f"{rel}/{ch}/{arch}")

    rel_rows = await db.execute(select(Release))
    for rel in rel_rows.scalars().all():
        releasename = (rel.releasename or "").strip()
        if not releasename:
            continue
        branches = [r2_publish_branch(rel), "testing", "stable"]
        arches = rel.arch_list() or ["x86_64", "aarch64"]
        for br in branches:
            b = (br or "").strip()
            if not b:
                continue
            for arch in arches:
                a = (arch or "").strip()
                if not a:
                    continue
                key = (releasename, b, a)
                if key in targets:
                    continue
                targets[key] = RepoTarget(
                    releasename=releasename,
                    branch=b,
                    arch=a,
                    prefix=f"{releasename}/{b}/{a}",
                )
    return sorted(targets.values(), key=lambda x: (x.releasename, x.branch, x.arch))


async def _external_provides_for_release_arch(
    baseurls: list[str],
    arch: str,
    timeout_s: float = 12.0,
) -> set[str]:
    out: set[str] = set()
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        for base in baseurls:
            u = (base or "").strip()
            if not u:
                continue
            u = u.replace("$basearch", arch)
            repomd_url = u.rstrip("/") + "/repodata/repomd.xml"
            try:
                r = await client.get(repomd_url)
                if r.status_code != 200:
                    continue
                href = _resolve_primary_href(r.content)
                if not href:
                    continue
                purl = href if href.startswith("http") else urljoin(repomd_url, href)
                rp = await client.get(purl)
                if rp.status_code != 200:
                    continue
                primary_xml = _decompress_if_needed(rp.content, purl)
                ext_provides, ext_sources = _build_provides_index(primary_xml)
                out.update(ext_provides.keys())
                out.update(ext_sources)
            except Exception:
                logger.debug("skip external repo url=%s arch=%s", repomd_url, arch, exc_info=True)
    return out


def _deps_graph_cache_ttl_sec() -> int:
    raw = (os.environ.get("OREON_DEPS_GRAPH_CACHE_SEC") or "120").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 120
    return max(0, min(n, 3600))


def _deps_graph_max_concurrent() -> int:
    raw = (os.environ.get("OREON_DEPS_GRAPH_MAX_CONCURRENT") or "4").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 4
    return max(1, min(n, 16))


async def build_dependencies_graph(db: AsyncSession, *, bypass_cache: bool = False) -> dict[str, Any]:
    global _deps_graph_cache_at, _deps_graph_cache_payload
    ttl = _deps_graph_cache_ttl_sec()
    now_mono = time.monotonic()
    if (
        not bypass_cache
        and ttl > 0
        and _deps_graph_cache_payload is not None
        and (now_mono - _deps_graph_cache_at) < ttl
    ):
        out = dict(_deps_graph_cache_payload)
        out["cached"] = True
        out["cache_ttl_sec"] = ttl
        out["cache_age_sec"] = int(now_mono - _deps_graph_cache_at)
        return out

    targets = await _collect_repo_targets(db)
    rel_rows = await db.execute(select(Release))
    releases = rel_rows.scalars().all()
    rel_by_name = {(r.releasename or "").strip(): r for r in releases if (r.releasename or "").strip()}
    rr = await db.execute(select(ReleaseRepo).where(ReleaseRepo.enabled.is_(True)))
    baseurl_by_release_name: dict[str, list[str]] = {}
    for row in rr.scalars().all():
        rel_name = ""
        if getattr(row, "release", None) is not None:
            rel_name = (getattr(row.release, "releasename", None) or "").strip()
        if not rel_name:
            rel_obj = await db.get(Release, row.release_id)
            rel_name = (rel_obj.releasename or "").strip() if rel_obj else ""
        if not rel_name:
            continue
        baseurl_by_release_name.setdefault(rel_name, []).append((row.baseurl or "").strip())

    ext_cache: dict[tuple[str, str], set[str]] = {}
    sem = asyncio.Semaphore(_deps_graph_max_concurrent())

    async def _analyze_one(t: RepoTarget) -> dict[str, Any]:
        rk = (t.releasename, t.arch)
        async with sem:
            if rk not in ext_cache:
                ext_cache[rk] = await _external_provides_for_release_arch(
                    baseurl_by_release_name.get(t.releasename, []),
                    t.arch,
                )
            return await asyncio.to_thread(_analyze_repo, t, ext_cache[rk])

    results = await asyncio.gather(*[_analyze_one(t) for t in targets])

    releases_set: set[str] = set()
    branches: set[str] = set()
    package_names: set[str] = set()
    nodes_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    all_edges: list[dict[str, Any]] = []
    all_missing: list[dict[str, Any]] = []
    all_runtime_edges: list[dict[str, Any]] = []
    all_runtime_missing: list[dict[str, Any]] = []
    repo_errors: list[dict[str, str]] = []

    for result in results:
        target: RepoTarget = result["repo"]
        releases_set.add(target.releasename)
        branches.add(target.branch)
        if result.get("error"):
            repo_errors.append({"repo": target.key, "error": str(result["error"])})
            continue
        pkgs: dict[str, dict[str, Any]] = result["packages"]
        for pkg_name in pkgs:
            package_names.add(pkg_name)
            nk = (target.releasename, target.branch, pkg_name)
            cur = nodes_map.get(nk)
            if cur is None:
                cur = {
                    "id": f"{target.releasename}|{target.branch}|{pkg_name}",
                    "release": target.releasename,
                    "branch": target.branch,
                    "name": pkg_name,
                    "arches": [],
                    "missing_count": 0,
                }
                nodes_map[nk] = cur
            if target.arch not in cur["arches"]:
                cur["arches"].append(target.arch)

        for m in result["missing"]:
            all_missing.append(m)
            nk = (m["release"], m["branch"], m["package"])
            cur = nodes_map.get(nk)
            if cur is not None:
                cur["missing_count"] = int(cur.get("missing_count") or 0) + 1

        all_edges.extend(result["edges"])
        all_runtime_edges.extend(result.get("runtime_edges") or [])
        all_runtime_missing.extend(result.get("runtime_missing") or [])

    all_missing.sort(key=lambda x: (x["release"], x["branch"], x["package"], x["requirement"], x["arch"]))
    all_runtime_missing.sort(key=lambda x: (x["release"], x["branch"], x["package"], x["requirement"], x["arch"]))
    nodes = sorted(nodes_map.values(), key=lambda x: (x["release"], x["branch"], x["name"]))
    for n in nodes:
        n["arches"] = sorted(n["arches"])

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "repositories_scanned": len(targets),
            "repositories_with_errors": len(repo_errors),
            "nodes": len(nodes),
            "edges": len(all_edges),
            "missing_requirements": len(all_missing),
            "runtime_edges": len(all_runtime_edges),
            "runtime_missing_requirements": len(all_runtime_missing),
            "buildroot_blockers": 0,
        },
        "releases": sorted(releases_set),
        "branches": sorted(branches),
        "packages": sorted(package_names),
        "nodes": nodes,
        "edges": all_edges,
        "missing": all_missing,
        "runtime_edges": all_runtime_edges,
        "runtime_missing": all_runtime_missing,
        "buildroot_missing": [],
        "repo_errors": repo_errors,
        "cached": False,
        "cache_ttl_sec": ttl,
    }
    if ttl > 0:
        _deps_graph_cache_payload = dict(out)
        _deps_graph_cache_payload.pop("cached", None)
        _deps_graph_cache_at = time.monotonic()
    return out
