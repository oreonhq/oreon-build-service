from __future__ import annotations

import bz2
import gzip
import logging
import lzma
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from oreon_build.services.r2 import get_r2_client

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 300.0
_cache: dict[tuple[str, str, str], tuple[float, RepoCapabilityIndex]] = {}


@dataclass
class RepoCapabilityIndex:
    source_names: set[str] = field(default_factory=set)
    binary_names: set[str] = field(default_factory=set)
    provides: set[str] = field(default_factory=set)

    def has_capability(self, name: str) -> bool:
        n = (name or "").strip()
        if not n:
            return False
        if n in self.provides or n in self.binary_names or n in self.source_names:
            return True
        low = n.lower()
        return any(x.lower() == low for x in self.provides | self.binary_names | self.source_names)

    def satisfies_source(self, name: str) -> bool:
        n = (name or "").strip()
        return bool(n) and (n in self.source_names or n in self.binary_names or self.has_capability(n))


def _decompress_if_needed(raw: bytes, key: str) -> bytes:
    lk = key.lower()
    if lk.endswith(".gz"):
        return gzip.decompress(raw)
    if lk.endswith(".bz2"):
        return bz2.decompress(raw)
    if lk.endswith(".xz"):
        return lzma.decompress(raw)
    return raw


def _resolve_primary_key(repo_prefix: str, repomd_raw: bytes) -> str | None:
    try:
        root = ET.fromstring(repomd_raw)
    except ET.ParseError:
        return None
    for data in root.findall(".//{*}data"):
        if (data.attrib.get("type") or "") != "primary":
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


def _ingest_primary_xml(index: RepoCapabilityIndex, primary_xml: bytes) -> None:
    try:
        root = ET.fromstring(primary_xml)
    except ET.ParseError:
        return
    for pkg in root.findall(".//{*}package"):
        arch = (pkg.findtext("{*}arch") or "").strip()
        if arch == "src":
            continue
        name = (pkg.findtext("{*}name") or "").strip()
        if name:
            index.binary_names.add(name)
            index.provides.add(name)
        sourcerpm = (pkg.findtext(".//{*}sourcerpm") or "").strip()
        if sourcerpm.endswith(".src.rpm"):
            src = sourcerpm[:-8].rsplit("-", 2)[0]
            if src:
                index.source_names.add(src)
        for entry in pkg.findall(".//{*}provides/{*}entry"):
            prov = (entry.attrib.get("name") or "").strip()
            if prov:
                index.provides.add(prov)


def _load_arch_primary(releasename: str, branch: str, arch: str) -> RepoCapabilityIndex:
    partial = RepoCapabilityIndex()
    r2 = get_r2_client()
    repo_prefix = f"{releasename}/{branch}/{arch}"
    repomd = r2.get_object(f"{repo_prefix}/repodata/repomd.xml")
    if not repomd:
        return partial
    primary_key = _resolve_primary_key(repo_prefix, repomd)
    if not primary_key:
        return partial
    primary_raw = r2.get_object(primary_key)
    if not primary_raw:
        return partial
    _ingest_primary_xml(partial, _decompress_if_needed(primary_raw, primary_key))
    return partial


def load_repo_capability_index(
    releasename: str,
    branch: str,
    architectures: list[str],
) -> RepoCapabilityIndex:
    rel = (releasename or "").strip()
    br = (branch or "dev").strip()
    arch_key = ",".join(sorted({(a or "").strip() for a in architectures if (a or "").strip()}))
    cache_key = (rel, br, arch_key)
    now = time.monotonic()
    hit = _cache.get(cache_key)
    if hit and now - hit[0] < _CACHE_TTL_SEC:
        return hit[1]

    index = RepoCapabilityIndex()
    for arch in architectures:
        a = (arch or "").strip()
        if not a:
            continue
        part = _load_arch_primary(rel, br, a)
        index.binary_names.update(part.binary_names)
        index.provides.update(part.provides)
        index.source_names.update(part.source_names)

    _cache[cache_key] = (now, index)
    logger.info(
        "repo capability index %s/%s arches=%s sources=%s binaries=%s provides=%s",
        rel,
        br,
        arch_key or "(none)",
        len(index.source_names),
        len(index.binary_names),
        len(index.provides),
    )
    return index


def clear_repo_capability_cache() -> None:
    _cache.clear()
