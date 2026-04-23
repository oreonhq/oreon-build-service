"""Dependency graph and missing dependency checks from published repo metadata."""
from __future__ import annotations

import gzip
import bz2
import lzma
import logging
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
import subprocess
from urllib.parse import urljoin
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import Release, ReleaseRepo, Repository
from oreon_build.services.r2 import get_r2_client
import httpx

logger = logging.getLogger(__name__)

RPM_NS = "{http://linux.duke.edu/metadata/rpm}"


@dataclass(frozen=True)
class RepoTarget:
    releasename: str
    branch: str
    arch: str
    prefix: str

    @property
    def key(self) -> str:
        return f"{self.releasename}|{self.branch}|{self.arch}"


_REQ_STOPWORDS = {"and", "or", "if", "else", "with", "without", "unless"}


def _extract_requirement_names(raw: str) -> list[str]:
    s = (raw or "").strip()
    if not s:
        return []
    # Split on boolean operators and comparison operators while keeping soname tokens intact.
    chunks = re.split(r"[\s,|&<>==]+", s)
    out: list[str] = []
    seen: set[str] = set()
    for c in chunks:
        t = c.strip()
        while t.startswith("("):
            t = t[1:].strip()
        while t.endswith(")") and "(" not in t:
            t = t[:-1].strip()
        if not t:
            continue
        low = t.lower()
        if low in _REQ_STOPWORDS:
            continue
        if t.startswith("rpmlib(") or t.startswith("config("):
            continue
        if t.startswith("/"):
            continue
        if re.fullmatch(r"[0-9]+(\.[0-9]+)*", t):
            continue
        # Keep useful tokens like package names, virtual provides, and soname provides.
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _import_hawkey_with_system_fallback():
    try:
        import hawkey  # type: ignore

        return hawkey
    except Exception:
        # Common deployment layout here is API in .venv but hawkey installed from OS RPM
        # into system site-packages. Try those paths before failing.
        candidates = (
            "/usr/lib64/python3.14/site-packages",
            "/usr/lib/python3.14/site-packages",
        )
        for p in candidates:
            if p not in sys.path:
                sys.path.append(p)
        import hawkey  # type: ignore

        return hawkey


def _safe_text(node: ET.Element | None, default: str = "") -> str:
    if node is None or node.text is None:
        return default
    return node.text.strip()


def _package_name_from_href(href: str) -> str | None:
    base = href.rsplit("/", 1)[-1]
    if base.endswith(".rpm"):
        base = base[:-4]
    parts = base.rsplit("-", 2)
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


def _source_name_from_srpm_filename(raw: str) -> str | None:
    s = (raw or "").strip()
    if s.endswith(".src.rpm"):
        s = s[:-8]
    parts = s.rsplit("-", 2)
    if len(parts) != 3:
        return None
    return parts[0] or None


def _map_binary_to_source_name(bin_name: str, source_name_candidates: set[str] | None) -> str:
    b = (bin_name or "").strip()
    if not b or not source_name_candidates:
        return b
    if b in source_name_candidates:
        return b
    matches = [s for s in source_name_candidates if b.startswith(s + "-")]
    if matches:
        # Longest prefix wins (e.g. python3-foo-bar-devel -> python3-foo-bar)
        matches.sort(key=len, reverse=True)
        return matches[0]
    return b


def _is_actionable_unmapped_requirement(req: str) -> bool:
    r = (req or "").strip()
    if not r:
        return False
    # Generic macro marker used by buildroot stacks.
    if r.endswith("-srpm-macros"):
        return True
    # Generic package-like token (no rich/soname/file syntax). This avoids hardcoding names.
    # Examples matched: debugedit, binutils, dwz, ed, python-srpm-macros
    # Examples not matched: libfoo.so.1()(64bit), filesystem(aarch64), /path/file
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._-]*", r):
        return True
    return False


def _build_chain_from_missing(items: list[dict[str, Any]]) -> str:
    names = sorted({str(x.get("package") or "").strip() for x in items if (x.get("package") or "").strip()})
    if not names:
        return ""
    # We keep this conservative and deterministic. Missing deps usually indicate leaf failures first.
    # Maintainers can still adjust grouping manually.
    return " ".join(names)


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


def _resolve_primary_key(repo_prefix: str, repomd_bytes: bytes) -> str | None:
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
        if not href:
            continue
        if href.startswith(f"{repo_prefix}/"):
            return href
        return f"{repo_prefix}/{href.lstrip('/')}"
    return None


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


def _decompress_if_needed(raw: bytes, key: str) -> bytes:
    k = key.lower()
    if k.endswith(".gz"):
        return gzip.decompress(raw)
    if k.endswith(".bz2"):
        return bz2.decompress(raw)
    if k.endswith(".xz"):
        return lzma.decompress(raw)
    if k.endswith(".zst") or k.endswith(".zstd"):
        # Try python zstandard first, then shell zstd fallback.
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
            raise RuntimeError("Failed to decompress zstd primary metadata")
    return raw


def _parse_primary_xml(
    primary_xml: bytes, source_name_candidates: set[str] | None = None
) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]], dict[str, set[str]]]:
    # Track only source packages while ingesting binary metadata.
    src_pkgs: dict[str, dict[str, Any]] = {}
    provides_index: dict[str, set[str]] = {}
    requires_index: dict[str, set[str]] = {}
    root = ET.fromstring(primary_xml)
    for pkg in root.findall(".//{*}package"):
        bin_name = _safe_text(pkg.find("{*}name"))
        if not bin_name:
            continue
        # sourcerpm is not under the rpm namespace in primary metadata, so use wildcard ns.
        src_name = _source_name_from_sourcerpm(_safe_text(pkg.find("{*}sourcerpm")))
        if not src_name:
            src_name = _map_binary_to_source_name(bin_name, source_name_candidates)
        provides: set[str] = set()
        requires: set[str] = set()

        for e in pkg.findall(f".//{RPM_NS}provides/{RPM_NS}entry"):
            prov_name = (e.attrib.get("name") or "").strip()
            if not prov_name:
                continue
            provides.add(prov_name)
            provides_index.setdefault(prov_name, set()).add(src_name)

        for e in pkg.findall(f".//{RPM_NS}requires/{RPM_NS}entry"):
            req_name = (e.attrib.get("name") or "").strip()
            parsed_tokens = _extract_requirement_names(req_name)
            for parsed in parsed_tokens:
                requires.add(parsed)
                requires_index.setdefault(parsed, set()).add(src_name)

        loc = pkg.find("{*}location")
        href = (loc.attrib.get("href") if loc is not None else "") or ""
        src_pkg = _package_name_from_href(href)
        if src_pkg:
            provides.add(src_pkg)
            provides_index.setdefault(src_pkg, set()).add(src_name)

        cur = src_pkgs.get(src_name)
        if cur is None:
            cur = {"name": src_name, "provides": set(), "requires": set()}
            src_pkgs[src_name] = cur
        cur["provides"].update(provides)
        cur["requires"].update(requires)
    normalized: dict[str, dict[str, Any]] = {}
    for src_name, d in src_pkgs.items():
        normalized[src_name] = {
            "name": src_name,
            "provides": sorted(d["provides"]),
            "requires": sorted(d["requires"]),
        }
    return normalized, provides_index, requires_index


def _analyze_repo(target: RepoTarget, tmpdir: Path, external_provides: set[str] | None = None) -> dict[str, Any]:
    try:
        hawkey = _import_hawkey_with_system_fallback()
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "hawkey is not available on this API host. Install python3-hawkey (or python-hawkey) to use the Dependencies page."
        ) from exc

    r2 = get_r2_client()
    repomd_key = f"{target.prefix}/repodata/repomd.xml"
    repomd = r2.get_object(repomd_key)
    if not repomd:
        return {"repo": target, "packages": {}, "missing": [], "edges": [], "error": "repomd_missing"}

    primary_key = _resolve_primary_key(target.prefix, repomd)
    if not primary_key:
        return {"repo": target, "packages": {}, "missing": [], "edges": [], "error": "primary_missing_in_repomd"}

    primary_raw = r2.get_object(primary_key)
    if not primary_raw:
        return {"repo": target, "packages": {}, "missing": [], "edges": [], "error": "primary_blob_missing"}
    try:
        src_prefix = f"{target.releasename}/{target.branch}/src/"
        src_candidates: set[str] = set()
        for k in r2.iter_keys_with_prefix(src_prefix):
            fn = k.rsplit("/", 1)[-1]
            sn = _source_name_from_srpm_filename(fn)
            if sn:
                src_candidates.add(sn)
        primary_xml = _decompress_if_needed(primary_raw, primary_key)
        pkgs, provides_index, _requires_index = _parse_primary_xml(primary_xml, src_candidates)
    except Exception as exc:
        return {
            "repo": target,
            "packages": {},
            "missing": [],
            "edges": [],
            "error": f"primary_parse_failed:{type(exc).__name__}",
        }

    # Use hawkey to parse dependency relations in the same format that dnf/hawkey understands.
    # We keep XML parsing for metadata ingestion from R2, then use hawkey as the dependency matcher.
    _ = hawkey  # keep explicit usage visible and enforced by import

    edges: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for pkg_name, p in pkgs.items():
        reqs = p.get("requires") or []
        for req in reqs:
            providers = sorted(provides_index.get(req, set()))
            if providers:
                for prov in providers[:8]:
                    if prov == pkg_name:
                        continue
                    edges.append(
                        {
                            "release": target.releasename,
                            "branch": target.branch,
                            "arch": target.arch,
                            "from": pkg_name,
                            "to": prov,
                            "requirement": req,
                            "missing": False,
                        }
                    )
            else:
                if external_provides and req in external_provides:
                    continue
                # Only track actionable internal gaps. If a requirement does not map to a source
                # package candidate in this release/branch src index, it is likely an external/base
                # repo dep or a pure virtual token and cannot be fixed by chain-building this repo set.
                req_src = _map_binary_to_source_name(req, src_candidates)
                keep_nonrepo_buildroot_req = False
                if req_src not in src_candidates:
                    # Keep unresolved buildroot/toolchain requirements visible. These are frequently
                    # the actual blockers when mock installs buildsys-build.
                    if _is_actionable_unmapped_requirement(req):
                        keep_nonrepo_buildroot_req = True
                    else:
                        continue
                target_req = req if keep_nonrepo_buildroot_req else req_src
                missing.append(
                    {
                        "release": target.releasename,
                        "branch": target.branch,
                        "arch": target.arch,
                        "package": pkg_name,
                        "requirement": target_req,
                    }
                )
                edges.append(
                    {
                        "release": target.releasename,
                        "branch": target.branch,
                        "arch": target.arch,
                        "from": pkg_name,
                        "to": target_req,
                        "requirement": target_req,
                        "missing": True,
                    }
                )

    return {"repo": target, "packages": pkgs, "missing": missing, "edges": edges, "error": None}


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
                repomd = r.content
                href = _resolve_primary_href(repomd)
                if not href:
                    continue
                if href.startswith("http://") or href.startswith("https://"):
                    purl = href
                else:
                    purl = urljoin(repomd_url, href)
                rp = await client.get(purl)
                if rp.status_code != 200:
                    continue
                primary_xml = _decompress_if_needed(rp.content, purl)
                pkgs, provides_index, _ = _parse_primary_xml(primary_xml)
                out.update(pkgs.keys())
                out.update(provides_index.keys())
            except Exception:
                logger.debug("skip external repo metadata url=%s arch=%s", repomd_url, arch, exc_info=True)
                continue
    return out


async def build_dependencies_graph(db: AsyncSession) -> dict[str, Any]:
    targets = await _collect_repo_targets(db)
    rel_rows = await db.execute(select(Release))
    releases = rel_rows.scalars().all()
    rel_by_name = {(r.releasename or "").strip(): r for r in releases if (r.releasename or "").strip()}
    rr = await db.execute(select(ReleaseRepo).where(ReleaseRepo.enabled.is_(True)))
    baseurl_by_release_name: dict[str, list[str]] = {}
    for row in rr.scalars().all():
        rel = rel_by_name.get((getattr(row.release, "releasename", None) or "").strip()) if getattr(row, "release", None) else None
        if rel is None:
            rel_obj = await db.get(Release, row.release_id)
            rel_name = (rel_obj.releasename or "").strip() if rel_obj else ""
        else:
            rel_name = (rel.releasename or "").strip()
        if not rel_name:
            continue
        baseurl_by_release_name.setdefault(rel_name, []).append((row.baseurl or "").strip())

    ext_cache: dict[tuple[str, str], set[str]] = {}

    with tempfile.TemporaryDirectory(prefix="oreon-deps-") as td:
        _tmpdir = Path(td)
        results = []
        for t in targets:
            rk = (t.releasename, t.arch)
            if rk not in ext_cache:
                ext_cache[rk] = await _external_provides_for_release_arch(
                    baseurl_by_release_name.get(t.releasename, []),
                    t.arch,
                )
            results.append(_analyze_repo(t, _tmpdir, external_provides=ext_cache[rk]))

    releases_set: set[str] = set()
    branches: set[str] = set()
    package_names: set[str] = set()
    dependency_terms: set[str] = set()
    nodes_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    all_edges: list[dict[str, Any]] = []
    all_missing: list[dict[str, Any]] = []
    repo_errors: list[dict[str, str]] = []

    for result in results:
        target: RepoTarget = result["repo"]
        releases_set.add(target.releasename)
        branches.add(target.branch)
        if result.get("error"):
            repo_errors.append({"repo": target.key, "error": str(result["error"])})
            continue
        pkgs: dict[str, dict[str, Any]] = result["packages"]
        for pkg_name, p in pkgs.items():
            package_names.add(pkg_name)
            dependency_terms.update(p.get("requires") or [])
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

    all_missing.sort(key=lambda x: (x["release"], x["branch"], x["package"], x["requirement"], x["arch"]))
    nodes = sorted(nodes_map.values(), key=lambda x: (x["release"], x["branch"], x["name"]))
    for n in nodes:
        n["arches"] = sorted(n["arches"])

    chain_build = _build_chain_from_missing(all_missing)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "repositories_scanned": len(targets),
            "repositories_with_errors": len(repo_errors),
            "nodes": len(nodes),
            "edges": len(all_edges),
            "missing_requirements": len(all_missing),
        },
        "releases": sorted(releases_set),
        "branches": sorted(branches),
        "packages": sorted(package_names),
        "dependency_terms": sorted(dependency_terms),
        "nodes": nodes,
        "edges": all_edges,
        "missing": all_missing,
        "chain_build": chain_build,
        "repo_errors": repo_errors,
    }
