from __future__ import annotations

import bz2
import gzip
import io
import logging
import lzma
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from oreon_build.core.rpm_requires import (
    is_arch_qualified_rpm_dependency,
    requirement_check_capabilities,
    rpm_requirement_primary,
)
from oreon_build.services.r2 import get_r2_client

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 300.0
_cache: dict[tuple[str, str, str], tuple[float, RepoCapabilityIndex]] = {}

_RE_RICH_IF = re.compile(r"^(.*?)\s+(if|unless)\s+(.*)$", re.IGNORECASE | re.DOTALL)
_RE_RICH_OR = re.compile(r"\s+or\s+", re.IGNORECASE)
_RE_RICH_AND = re.compile(r"\s+(?:and|with)\s+", re.IGNORECASE)
_RE_RICH_BOOL_SPLIT = re.compile(
    r"\s+(?:and|or|if|unless|with|without|else)\s+",
    re.IGNORECASE,
)
_RE_VERSION_OPS = re.compile(r"\s*(<=|>=|==|!=|<|>|=)\s*")


def is_rpmlib_requirement(req: str) -> bool:
    return (req or "").strip().lower().startswith("rpmlib(")


def is_rich_dependency(req: str) -> bool:
    r = (req or "").strip()
    return bool(r) and r.startswith("(") and r.endswith(")")


@dataclass
class RepoCapabilityIndex:
    source_names: set[str] = field(default_factory=set)
    binary_names: set[str] = field(default_factory=set)
    provides: set[str] = field(default_factory=set)
    capability_providers: dict[str, set[str]] = field(default_factory=dict)
    binary_requires: dict[str, set[str]] = field(default_factory=dict)
    binary_source: dict[str, str] = field(default_factory=dict)
    _caps_lower: set[str] = field(default_factory=set, repr=False)
    _providers_by_lower: dict[str, set[str]] = field(default_factory=dict, repr=False)
    _unsatisfied_cache: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)
    _all_caps: set[str] | None = field(default=None, repr=False)

    def finalize_lookups(self) -> None:
        self._all_caps = self.provides | self.binary_names | self.source_names
        self._caps_lower = {x.lower() for x in self._all_caps}
        merged: dict[str, set[str]] = {}
        for cap, pkgs in self.capability_providers.items():
            merged.setdefault(cap.lower(), set()).update(pkgs)
        self._providers_by_lower = merged
        self._unsatisfied_cache.clear()

    @property
    def all_caps(self) -> set[str]:
        if self._all_caps is None:
            self.finalize_lookups()
        return self._all_caps or set()

    def has_capability(self, name: str) -> bool:
        n = (name or "").strip()
        if not n:
            return False
        if is_rpmlib_requirement(n):
            return True
        if n in self.provides or n in self.binary_names or n in self.source_names:
            return True
        if not self._caps_lower:
            self.finalize_lookups()
        return n.lower() in self._caps_lower

    def satisfies_requirement(self, dep: str) -> bool:
        d = (dep or "").strip()
        if not d:
            return True
        if is_rpmlib_requirement(d):
            return True
        if is_rich_dependency(d):
            return self._satisfies_rich_dependency(d)
        for cap in requirement_check_capabilities(d):
            if self.has_capability(cap):
                return True
        return False

    def _leaf_capability_present(self, tok: str) -> bool:
        t = (tok or "").strip()
        if not t or is_rpmlib_requirement(t):
            return True
        if is_rich_dependency(t):
            return self._satisfies_rich_dependency(t)
        for cap in requirement_check_capabilities(t):
            if self.has_capability(cap):
                return True
        return False

    def _rich_side_present(self, side: str) -> bool:
        s = (side or "").strip()
        if not s:
            return True
        if is_rich_dependency(s):
            return self._satisfies_rich_dependency(s)
        primary = rpm_requirement_primary(s) or s
        return self._leaf_capability_present(primary)

    def _satisfies_rich_dependency(self, dep: str) -> bool:
        text = (dep or "").strip()
        if not text:
            return True
        inner = text[1:-1].strip() if is_rich_dependency(text) else text
        if not inner:
            return True

        m = _RE_RICH_IF.match(inner)
        if m:
            lhs, op, rhs = m.group(1).strip(), m.group(2).lower(), m.group(3).strip()
            cond = self._rich_side_present(rhs)
            if op == "if":
                return True if not cond else self._rich_side_present(lhs)
            return True if cond else self._rich_side_present(lhs)

        if _RE_RICH_OR.search(inner):
            return any(self._rich_side_present(p) for p in _RE_RICH_OR.split(inner))
        if _RE_RICH_AND.search(inner):
            return all(self._rich_side_present(p) for p in _RE_RICH_AND.split(inner))

        leaves = [
            tok
            for tok in _rich_dep_leaf_names(text)
            if tok and not is_rpmlib_requirement(tok)
        ]
        if not leaves:
            return True
        return any(self._leaf_capability_present(tok) for tok in leaves)

    def rich_dependency_missing_leaves(self, dep: str) -> list[str]:
        text = (dep or "").strip()
        if not text or self._satisfies_rich_dependency(text):
            return []
        inner = text[1:-1].strip() if is_rich_dependency(text) else text
        m = _RE_RICH_IF.match(inner)
        if m:
            lhs, op, rhs = m.group(1).strip(), m.group(2).lower(), m.group(3).strip()
            cond = self._rich_side_present(rhs)
            need_lhs = (op == "if" and cond) or (op == "unless" and not cond)
            if not need_lhs:
                return []
            if self._rich_side_present(lhs):
                return []
            return _rich_dep_actionable_leaves(lhs)

        if _RE_RICH_OR.search(inner):
            missing: list[str] = []
            for part in _RE_RICH_OR.split(inner):
                if self._rich_side_present(part):
                    return []
                missing.extend(_rich_dep_actionable_leaves(part))
            return _dedupe_caps(missing)

        if _RE_RICH_AND.search(inner):
            missing = []
            for part in _RE_RICH_AND.split(inner):
                if not self._rich_side_present(part):
                    missing.extend(_rich_dep_actionable_leaves(part))
            return _dedupe_caps(missing)

        return _dedupe_caps(
            [
                tok
                for tok in _rich_dep_leaf_names(text)
                if tok and not is_rpmlib_requirement(tok) and not self._leaf_capability_present(tok)
            ]
        )

    def satisfies_source(self, name: str) -> bool:
        n = (name or "").strip()
        if not n:
            return False
        if n in self.source_names or n in self.binary_names:
            return True
        return self.has_capability(n)

    def providers_for(self, cap: str) -> set[str]:
        c = (cap or "").strip()
        if not c:
            return set()
        if not self._providers_by_lower:
            self.finalize_lookups()
        out: set[str] = set()
        if c in self.binary_names:
            out.add(c)
        out.update(self.capability_providers.get(c, set()))
        out.update(self._providers_by_lower.get(c.lower(), set()))
        return out

    def transitive_unsatisfied(self, cap: str) -> set[str]:
        root = (cap or "").strip()
        if not root:
            return set()
        if not self._caps_lower:
            self.finalize_lookups()
        hit = self._unsatisfied_cache.get(root)
        if hit is not None:
            return set(hit)
        missing: set[str] = set()
        seen: set[str] = set()
        stack = [root]
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            if is_rpmlib_requirement(c):
                continue
            if is_rich_dependency(c):
                for leaf in self.rich_dependency_missing_leaves(c):
                    missing.add(leaf)
                continue
            if not self.has_capability(c):
                # arch-qualified names are install metadata; check base form too
                from oreon_build.core.rpm_requires import rpm_arch_qualified_base

                base = rpm_arch_qualified_base(c)
                if base and self.has_capability(base):
                    continue
                if any(self.has_capability(cap) for cap in requirement_check_capabilities(c)):
                    continue
                missing.add(c)
                continue
            pkgs = self.providers_for(c)
            if c in self.binary_requires:
                pkgs = pkgs | {c}
            for pkg in pkgs:
                for req in self.binary_requires.get(pkg, set()):
                    if req not in seen:
                        stack.append(req)
        result = frozenset(m for m in missing if not self.satisfies_requirement(m))
        self._unsatisfied_cache[root] = result
        return set(result)

    def binaries_requiring(self, cap: str) -> set[str]:
        c = (cap or "").strip()
        if not c:
            return set()
        return {pkg for pkg, reqs in self.binary_requires.items() if c in reqs}

    def broken_runtime_sources_for_missing_cap(self, cap: str) -> set[str]:
        c = (cap or "").strip()
        if not c or is_rpmlib_requirement(c) or is_rich_dependency(c):
            return set()
        out: set[str] = set()
        for pkg in self.binaries_requiring(c):
            src = self.binary_source.get(pkg)
            if src and self.satisfies_source(src):
                out.add(src)
        return out

    def _providers_to_follow(self, req: str, current_src: str | None) -> list[str]:
        r = (req or "").strip()
        if not r or is_rpmlib_requirement(r) or is_rich_dependency(r):
            return []
        if r in self.binary_names:
            return [r]
        provs = sorted(self.providers_for(r))
        if not provs:
            return []
        if current_src:
            same = [p for p in provs if self.binary_source.get(p) == current_src]
            if same:
                return same
        if len(provs) == 1:
            return provs
        return []

    def broken_runtime_sources_on_path(self, cap: str) -> set[str]:
        root = (cap or "").strip()
        if not root or is_rpmlib_requirement(root) or is_rich_dependency(root):
            return set()
        if not self._caps_lower:
            self.finalize_lookups()
        out: set[str] = set()
        seen_pkgs: set[str] = set()
        stack = list(self.providers_for(root))
        if root in self.binary_requires:
            stack.append(root)
        while stack:
            pkg = stack.pop()
            if pkg in seen_pkgs:
                continue
            seen_pkgs.add(pkg)
            current_src = self.binary_source.get(pkg)
            for req in self.binary_requires.get(pkg, set()):
                if is_rpmlib_requirement(req):
                    continue
                if is_rich_dependency(req):
                    if self.rich_dependency_missing_leaves(req):
                        if current_src and self.satisfies_source(current_src):
                            out.add(current_src)
                    continue
                if not self.satisfies_requirement(req):
                    if current_src and self.satisfies_source(current_src):
                        out.add(current_src)
                    continue
                for nxt in self._providers_to_follow(req, current_src):
                    if nxt not in seen_pkgs:
                        stack.append(nxt)
        return out

    def missing_runtime_caps_for_requirement(self, cap: str) -> set[str]:
        return set(self.transitive_unsatisfied(cap))

    def distgit_sources_for_capability(self, cap: str) -> set[str]:
        c = (cap or "").strip()
        if not c or self.satisfies_source(c) or is_rpmlib_requirement(c) or is_rich_dependency(c):
            return set()
        out: set[str] = set()
        for pkg in self.providers_for(c):
            src = self.binary_source.get(pkg)
            if src and not self.satisfies_source(src):
                out.add(src)
        return out


def _dedupe_caps(caps: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for cap in caps:
        c = (cap or "").strip()
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def _strip_version_ops(tok: str) -> str | None:
    t = (tok or "").strip()
    if not t:
        return None
    if t in {"<", ">", "=", "<=", ">=", "==", "!="}:
        return None
    parts = _RE_VERSION_OPS.split(t, maxsplit=1)
    base = (parts[0] if parts else t).strip()
    if not base or base in {"<", ">", "=", "<=", ">=", "==", "!="}:
        return None
    if re.fullmatch(r"[\d._~^:+-]+", base):
        return None
    return base or None


def _rich_dep_leaf_names(dep: str) -> list[str]:
    text = (dep or "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    text = text.replace("||", " ").replace("&&", " ")
    text = _RE_RICH_BOOL_SPLIT.sub(" ", text)
    out: list[str] = []
    skip_version = False
    for part in text.split():
        tok = part.strip()
        if not tok:
            continue
        if tok in {"<", ">", "=", "<=", ">=", "==", "!="}:
            skip_version = True
            continue
        if skip_version:
            skip_version = False
            continue
        cleaned = _strip_version_ops(tok)
        if cleaned:
            out.append(cleaned)
    return out


def _rich_dep_actionable_leaves(side: str) -> list[str]:
    s = (side or "").strip()
    if not s:
        return []
    if is_rich_dependency(s):
        return _dedupe_caps(_rich_dep_leaf_names(s))
    primary = rpm_requirement_primary(s) or s
    cleaned = _strip_version_ops(primary) or primary
    if not cleaned or is_rpmlib_requirement(cleaned):
        return []
    if is_arch_qualified_rpm_dependency(cleaned):
        from oreon_build.core.rpm_requires import rpm_arch_qualified_base

        base = rpm_arch_qualified_base(cleaned)
        return [base] if base else []
    return [cleaned]


def _decompress_if_needed(raw: bytes, key: str) -> bytes:
    lk = key.lower()
    if lk.endswith(".gz"):
        return gzip.decompress(raw)
    if lk.endswith(".bz2"):
        return bz2.decompress(raw)
    if lk.endswith(".xz"):
        return lzma.decompress(raw)
    if lk.endswith(".zst"):
        import zstandard as zstd

        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(io.BytesIO(raw)) as reader:
            return reader.read()
    return raw


def _resolve_repodata_key(repo_prefix: str, repomd_raw: bytes, data_type: str) -> str | None:
    try:
        root = ET.fromstring(repomd_raw)
    except ET.ParseError:
        return None
    for data in root.findall(".//{*}data"):
        if (data.attrib.get("type") or "") != data_type:
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


def _resolve_primary_key(repo_prefix: str, repomd_raw: bytes) -> str | None:
    return _resolve_repodata_key(repo_prefix, repomd_raw, "primary")


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
            index.capability_providers.setdefault(name, set()).add(name)
        sourcerpm = (pkg.findtext(".//{*}sourcerpm") or "").strip()
        if sourcerpm.endswith(".src.rpm") and name:
            src = sourcerpm[:-8].rsplit("-", 2)[0]
            if src:
                index.source_names.add(src)
                index.binary_source[name] = src
        for entry in pkg.findall(".//{*}provides/{*}entry"):
            prov = (entry.attrib.get("name") or "").strip()
            if prov:
                index.provides.add(prov)
                if name:
                    index.capability_providers.setdefault(prov, set()).add(name)
        for entry in pkg.findall(".//{*}requires/{*}entry"):
            req = (entry.attrib.get("name") or "").strip()
            if req and name:
                index.binary_requires.setdefault(name, set()).add(req)
        for fnode in pkg.findall(".//{*}file"):
            path = (fnode.text or "").strip()
            if path and name:
                index.provides.add(path)
                index.capability_providers.setdefault(path, set()).add(name)


def _ingest_filelists_xml(index: RepoCapabilityIndex, filelists_xml: bytes) -> None:
    try:
        context = ET.iterparse(io.BytesIO(filelists_xml), events=("end",))
    except ET.ParseError:
        return
    for _event, elem in context:
        if not elem.tag.endswith("package"):
            continue
        name = (elem.attrib.get("name") or "").strip()
        arch = (elem.attrib.get("arch") or "").strip()
        if not name or arch == "src":
            continue
        for fnode in elem.findall("{*}file"):
            path = (fnode.text or "").strip()
            if not path:
                continue
            index.provides.add(path)
            index.capability_providers.setdefault(path, set()).add(name)
        elem.clear()


def _load_arch_metadata(releasename: str, branch: str, arch: str) -> RepoCapabilityIndex:
    partial = RepoCapabilityIndex()
    r2 = get_r2_client()
    repo_prefix = f"{releasename}/{branch}/{arch}"
    repomd = r2.get_object(f"{repo_prefix}/repodata/repomd.xml")
    if not repomd:
        return partial

    primary_key = _resolve_repodata_key(repo_prefix, repomd, "primary")
    if primary_key:
        primary_raw = r2.get_object(primary_key)
        if primary_raw:
            _ingest_primary_xml(partial, _decompress_if_needed(primary_raw, primary_key))

    filelists_key = _resolve_repodata_key(repo_prefix, repomd, "filelists")
    if filelists_key:
        filelists_raw = r2.get_object(filelists_key)
        if filelists_raw:
            _ingest_filelists_xml(partial, _decompress_if_needed(filelists_raw, filelists_key))

    return partial


def _load_arch_primary(releasename: str, branch: str, arch: str) -> RepoCapabilityIndex:
    return _load_arch_metadata(releasename, branch, arch)


def _merge_arch_indexes(parts: list[RepoCapabilityIndex]) -> RepoCapabilityIndex:
    index = RepoCapabilityIndex()
    if not parts:
        index.finalize_lookups()
        return index
    if len(parts) == 1:
        part = parts[0]
        index.binary_names.update(part.binary_names)
        index.provides.update(part.provides)
        index.source_names.update(part.source_names)
        index.capability_providers.update({k: set(v) for k, v in part.capability_providers.items()})
        index.binary_requires.update({k: set(v) for k, v in part.binary_requires.items()})
        index.binary_source.update(part.binary_source)
        index.finalize_lookups()
        return index

    common_bins = set(parts[0].binary_names)
    common_provides = set(parts[0].provides)
    common_sources = set(parts[0].source_names)
    for part in parts[1:]:
        common_bins &= part.binary_names
        common_provides &= part.provides
        common_sources &= part.source_names
    index.binary_names = common_bins
    index.provides = common_provides
    index.source_names = common_sources
    for part in parts:
        for cap, pkgs in part.capability_providers.items():
            index.capability_providers.setdefault(cap, set()).update(pkgs)
        for pkg, reqs in part.binary_requires.items():
            index.binary_requires.setdefault(pkg, set()).update(reqs)
        index.binary_source.update(part.binary_source)
    index.finalize_lookups()
    return index


def load_repo_capability_index(
    releasename: str,
    branch: str,
    architectures: list[str],
) -> RepoCapabilityIndex:
    """
    Multi-arch index. A capability is only "satisfied" if every target arch has it.
    Unioning arches was wrong: aarch64-only dbus-c++ made x86_64 plans skip the BR.
    """
    rel = (releasename or "").strip()
    br = (branch or "dev").strip()
    arches = sorted({(a or "").strip() for a in architectures if (a or "").strip()})
    arch_key = ",".join(arches)
    cache_key = (rel, br, arch_key)
    now = time.monotonic()
    hit = _cache.get(cache_key)
    if hit and now - hit[0] < _CACHE_TTL_SEC:
        return hit[1]

    parts = [_load_arch_metadata(rel, br, a) for a in arches]
    index = _merge_arch_indexes(parts)
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
