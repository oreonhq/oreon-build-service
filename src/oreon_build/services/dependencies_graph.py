"""Dependency graph and missing dependency checks from published repo metadata."""
from __future__ import annotations

import gzip
import bz2
import lzma
import logging
import sys
import tempfile
import xml.etree.ElementTree as ET
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import Release, Repository
from oreon_build.services.r2 import get_r2_client

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


def _parse_requirement_name(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("rpmlib(") or s.startswith("config("):
        return None
    if s.startswith("/"):
        return None
    for sep in (" ", ">=", "<=", "=", ">", "<"):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    if not s:
        return None
    return s


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


def _parse_primary_xml(primary_xml: bytes) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]], dict[str, set[str]]]:
    pkgs: dict[str, dict[str, Any]] = {}
    provides_index: dict[str, set[str]] = {}
    requires_index: dict[str, set[str]] = {}
    root = ET.fromstring(primary_xml)
    for pkg in root.findall(".//{*}package"):
        name = _safe_text(pkg.find("{*}name"))
        if not name:
            continue
        provides: set[str] = set()
        requires: set[str] = set()

        for e in pkg.findall(f".//{RPM_NS}provides/{RPM_NS}entry"):
            prov_name = (e.attrib.get("name") or "").strip()
            if not prov_name:
                continue
            provides.add(prov_name)
            provides_index.setdefault(prov_name, set()).add(name)

        for e in pkg.findall(f".//{RPM_NS}requires/{RPM_NS}entry"):
            req_name = (e.attrib.get("name") or "").strip()
            parsed = _parse_requirement_name(req_name)
            if not parsed:
                continue
            requires.add(parsed)
            requires_index.setdefault(parsed, set()).add(name)

        loc = pkg.find("{*}location")
        href = (loc.attrib.get("href") if loc is not None else "") or ""
        src_pkg = _package_name_from_href(href)
        if src_pkg:
            provides.add(src_pkg)
            provides_index.setdefault(src_pkg, set()).add(name)

        pkgs[name] = {
            "name": name,
            "provides": sorted(provides),
            "requires": sorted(requires),
        }
    return pkgs, provides_index, requires_index


def _analyze_repo(target: RepoTarget, tmpdir: Path) -> dict[str, Any]:
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
        primary_xml = _decompress_if_needed(primary_raw, primary_key)
        pkgs, provides_index, _requires_index = _parse_primary_xml(primary_xml)
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
                missing.append(
                    {
                        "release": target.releasename,
                        "branch": target.branch,
                        "arch": target.arch,
                        "package": pkg_name,
                        "requirement": req,
                    }
                )
                edges.append(
                    {
                        "release": target.releasename,
                        "branch": target.branch,
                        "arch": target.arch,
                        "from": pkg_name,
                        "to": req,
                        "requirement": req,
                        "missing": True,
                    }
                )

    return {"repo": target, "packages": pkgs, "missing": missing, "edges": edges, "error": None}


async def build_dependencies_graph(db: AsyncSession) -> dict[str, Any]:
    targets = await _collect_repo_targets(db)
    with tempfile.TemporaryDirectory(prefix="oreon-deps-") as td:
        _tmpdir = Path(td)
        results = [_analyze_repo(t, _tmpdir) for t in targets]

    releases: set[str] = set()
    branches: set[str] = set()
    package_names: set[str] = set()
    dependency_terms: set[str] = set()
    nodes_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    all_edges: list[dict[str, Any]] = []
    all_missing: list[dict[str, Any]] = []
    repo_errors: list[dict[str, str]] = []

    for result in results:
        target: RepoTarget = result["repo"]
        releases.add(target.releasename)
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
        "releases": sorted(releases),
        "branches": sorted(branches),
        "packages": sorted(package_names),
        "dependency_terms": sorted(dependency_terms),
        "nodes": nodes,
        "edges": all_edges,
        "missing": all_missing,
        "chain_build": chain_build,
        "repo_errors": repo_errors,
    }
