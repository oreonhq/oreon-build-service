# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import logging
import lzma
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oreon_build.core.rpm_requires import (
    evr_satisfies,
    install_check_capabilities,
    is_arch_qualified_rpm_dependency,
    is_distgit_source_name,
    parse_versioned_requirement,
    requirement_check_capabilities,
    rpm_evr_compare,
    rpm_requirement_primary,
    split_requirement_evr,
)
from oreon_build.services.r2 import get_r2_client

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 600.0
# primary-only indexes are ~hundreds of MB, not multi-GB filelist maps
_CACHE_MAX = 2
_cache: dict[tuple[str, str, str], tuple[float, RepoCapabilityIndex]] = {}
_cache_lock = threading.Lock()
# filelists belong in libsolv. putting every path into this Python index OOMs the API.
_INCLUDE_FILELISTS = os.environ.get("OREON_CAP_INCLUDE_FILELISTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

_RE_REPO_CHANNEL = re.compile(
    r"/(?P<rel>[^/]+)/(?P<channel>dev|testing|stable)/(?:\$basearch|\$arch|x86_64|aarch64|noarch|src)(?:/|$)",
    re.IGNORECASE,
)


_REPO_CHANNELS = ("dev", "testing", "stable")


def default_capability_extra_branches(publish_branch: str | None) -> list[str]:
    pub = (publish_branch or "dev").strip() or "dev"
    return [c for c in _REPO_CHANNELS if c != pub]


def repo_channels_from_mock_urls(releasename: str, urls: list[str] | None) -> list[str]:
    rel = (releasename or "").strip().strip("/")
    if not rel or not urls:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        u = str(raw or "").strip()
        if not u:
            continue
        m = _RE_REPO_CHANNEL.search(u)
        if not m:
            continue
        if (m.group("rel") or "").strip() != rel:
            continue
        ch = (m.group("channel") or "").strip().lower()
        if not ch or ch in seen:
            continue
        seen.add(ch)
        out.append(ch)
    return out

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
    # union across arches. skip/rebuild uses these so ExclusiveArch pkgs already
    # on one arch are not treated as missing for the whole multi-arch plan.
    source_names_any: set[str] = field(default_factory=set)
    binary_names_any: set[str] = field(default_factory=set)
    provides: set[str] = field(default_factory=set)
    capability_providers: dict[str, set[str]] = field(default_factory=dict)
    capability_evrs: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    binary_requires: dict[str, set[str]] = field(default_factory=dict)
    binary_source: dict[str, str] = field(default_factory=dict)
    _caps_lower: set[str] = field(default_factory=set, repr=False)
    _providers_by_lower: dict[str, set[str]] = field(default_factory=dict, repr=False)
    _unsatisfied_cache: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)
    _source_evrs: dict[str, tuple[tuple[str, str, str], ...]] = field(default_factory=dict, repr=False)
    _all_caps: set[str] | None = field(default=None, repr=False)

    def finalize_lookups(self) -> None:
        self._all_caps = self.provides | self.binary_names | self.source_names
        self._caps_lower = {x.lower() for x in self._all_caps}
        merged: dict[str, set[str]] = {}
        for cap, pkgs in self.capability_providers.items():
            merged.setdefault(cap.lower(), set()).update(pkgs)
        self._providers_by_lower = merged
        source_evrs: dict[str, set[tuple[str, str, str]]] = {}
        for source in self.source_names | self.source_names_any:
            evrs = self.capability_evrs.get(source) or ()
            if evrs:
                source_evrs.setdefault(source, set()).update(evrs)
        for binary, source in self.binary_source.items():
            evrs = self.capability_evrs.get(binary) or ()
            if evrs:
                source_evrs.setdefault(source, set()).update(evrs)
        self._source_evrs = {
            source: tuple(evrs)
            for source, evrs in source_evrs.items()
        }
        self._unsatisfied_cache.clear()

    @property
    def all_caps(self) -> set[str]:
        if self._all_caps is None:
            self.finalize_lookups()
        return self._all_caps or set()

    def has_capability(self, name: str) -> bool:
        """Exact capability match (dnf installability). No casefold."""
        n = (name or "").strip()
        if not n:
            return False
        if is_rpmlib_requirement(n):
            return True
        if self._all_caps is None:
            self.finalize_lookups()
        if n in self.provides or n in self.binary_names or n in self.source_names:
            return True
        return False

    def has_capability_ci(self, name: str) -> bool:
        """Case-insensitive presence. Discovery only, not installability."""
        n = (name or "").strip()
        if not n:
            return False
        if is_rpmlib_requirement(n):
            return True
        if self.has_capability(n):
            return True
        if not self._caps_lower:
            self.finalize_lookups()
        return n.lower() in self._caps_lower

    def _capability_evrs_for(self, name: str) -> list[tuple[str, str, str]]:
        n = (name or "").strip()
        if not n:
            return []
        return list(self.capability_evrs.get(n) or [])

    def satisfies_requirement(self, dep: str) -> bool:
        d = (dep or "").strip()
        if not d:
            return True
        if is_rpmlib_requirement(d):
            return True
        if is_rich_dependency(d):
            return self._satisfies_rich_dependency(d)
        name, op, ver = parse_versioned_requirement(d)
        if not name:
            return False
        if not self.has_capability(name):
            if op is None:
                for cap in install_check_capabilities(d):
                    if self.has_capability(cap):
                        return True
            return False
        if op is None or ver is None:
            return True
        need = split_requirement_evr(ver)
        evrs = self._capability_evrs_for(name)
        if not evrs:
            # name exists but no EVR recorded - treat versioned dep as unmet
            return False
        return any(evr_satisfies(op, evr, need) for evr in evrs)

    def providers_for(self, cap: str, *, exact: bool = True) -> set[str]:
        c = (cap or "").strip()
        if not c:
            return set()
        if not self._providers_by_lower:
            self.finalize_lookups()
        out: set[str] = set()
        if c in self.binary_names:
            out.add(c)
        out.update(self.capability_providers.get(c, set()))
        if not exact:
            out.update(self._providers_by_lower.get(c.lower(), set()))
        return out

    def _leaf_capability_present(self, tok: str) -> bool:
        t = (tok or "").strip()
        if not t or is_rpmlib_requirement(t):
            return True
        if is_rich_dependency(t):
            return self._satisfies_rich_dependency(t)
        for cap in install_check_capabilities(t):
            if self.has_capability(cap):
                return True
        return False

    def _rich_side_present(self, side: str) -> bool:
        s = (side or "").strip()
        if not s:
            return True
        if is_rich_dependency(s):
            return self._satisfies_rich_dependency(s)
        name, op, ver = parse_versioned_requirement(s)
        if name and op is not None and ver is not None:
            # EVR when we have it. No EVR metadata -> presence only (arch if deps).
            evrs = self._capability_evrs_for(name)
            if evrs:
                return self.satisfies_requirement(s)
            return self._leaf_capability_present(s)
        return self._leaf_capability_present(s)

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
        if n in self.source_names_any or n in self.binary_names_any:
            return True
        return self.has_capability(n)

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
                # arch-qualified names are install metadata check base form too
                from oreon_build.core.rpm_requires import rpm_arch_qualified_base

                base = rpm_arch_qualified_base(c)
                if base and self.has_capability(base):
                    continue
                if any(self.has_capability(cap) for cap in requirement_check_capabilities(c)):
                    continue
                # arch-partial binary: still walk Requires from the arches that have it
                pkgs = set(self.providers_for(c))
                if c in self.binary_requires:
                    pkgs.add(c)
                walked = False
                for pkg in pkgs:
                    reqs = self.binary_requires.get(pkg, set())
                    if not reqs:
                        continue
                    walked = True
                    for req in reqs:
                        if req not in seen:
                            stack.append(req)
                if walked or self.has_capability_any_arch(c):
                    missing.add(c)
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

    def missing_caps_on_provider_path(self, cap: str) -> set[str]:
        """Unsatisfied Requires reached from preferred providers of cap (dnf install path)."""
        root = (cap or "").strip()
        if not root or is_rpmlib_requirement(root) or is_rich_dependency(root):
            return set()
        if not self._caps_lower:
            self.finalize_lookups()
        missing: set[str] = set()
        if not self.satisfies_requirement(root):
            if is_rich_dependency(root):
                missing.update(self.rich_dependency_missing_leaves(root))
            else:
                missing.add(root)
            return missing
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
                    missing.update(self.rich_dependency_missing_leaves(req))
                    continue
                if not self.satisfies_requirement(req):
                    missing.add(req)
                    continue
                for nxt in self._providers_to_follow(req, current_src):
                    if nxt not in seen_pkgs:
                        stack.append(nxt)
        return missing

    def missing_runtime_caps_for_requirement(self, cap: str) -> set[str]:
        return set(self.missing_caps_on_provider_path(cap) or self.transitive_unsatisfied(cap))

    def has_capability_any_arch(self, name: str) -> bool:
        """True when primary metadata saw this on at least one arch/channel."""
        n = (name or "").strip()
        if not n:
            return False
        if self.has_capability(n):
            return True
        if n in self.binary_source or n in self.binary_requires:
            return True
        if self.capability_providers.get(n):
            return True
        return False

    def owner_sources_for_capability(self, cap: str) -> set[str]:
        """Source packages owning a provide, including published uninstallable ones."""
        c = (cap or "").strip()
        if not c or is_rpmlib_requirement(c) or is_rich_dependency(c):
            return set()
        out: set[str] = set()
        direct = self.binary_source.get(c)
        if direct and is_distgit_source_name(direct):
            out.add(direct)
        for pkg in self.providers_for(c):
            src = self.binary_source.get(pkg)
            if src and is_distgit_source_name(src):
                out.add(src)
            elif is_distgit_source_name(pkg) and (
                pkg in self.source_names or pkg in self.binary_source.values()
            ):
                out.add(pkg)
        return out

    def distgit_sources_for_capability(self, cap: str) -> set[str]:
        """Source packages that can produce an unmet capability.

        Includes already-published sources. Needed when a subpackage exists on only
        some arches (intersection drops it) or an EVR bump is required.
        """
        c = (cap or "").strip()
        if not c or is_rpmlib_requirement(c) or is_rich_dependency(c):
            return set()
        if self.has_capability(c):
            return set()
        return self.owner_sources_for_capability(c)

    def source_present_in_repo(self, name: str) -> bool:
        n = (name or "").strip()
        if not n:
            return False
        if n in self.source_names or n in self.source_names_any:
            return True
        if n in self.binary_names or n in self.binary_names_any:
            return True
        return any(src == n for src in self.binary_source.values())

    def source_present_for_arches(self, name: str) -> bool:
        """Present on every arch this index covers, not merely somewhere.

        source_present_in_repo folds in the any-arch union. A compiler built only
        on x86_64 then reads as present, so an aarch64 build never gets one and
        every consumer on that arch dies with nothing to link against.
        """
        n = (name or "").strip()
        if not n:
            return False
        return n in self.source_names or n in self.binary_names

    def _source_repo_evrs(self, name: str) -> list[tuple[str, str, str]]:
        n = (name or "").strip()
        if not n:
            return []
        if not self._source_evrs:
            self.finalize_lookups()
        return list(self._source_evrs.get(n) or ())

    def source_is_repo_current(
        self,
        name: str,
        epoch: int | str = 0,
        version: str = "",
        release: str = "",
        *,
        require_evr: bool = False,
    ) -> bool:
        """True when repo already has this source at EVR >= distgit EVR.

        Without distgit version: presence counts as current unless require_evr=True
        (used when deciding whether to rebuild a provider for an unmet capability).
        """
        n = (name or "").strip()
        if not n or not self.source_present_in_repo(n):
            return False
        dist_ver = (version or "").strip()
        dist_rel = (release or "").strip()
        if not dist_ver:
            return not require_evr
        try:
            dist_epoch = str(int(epoch or 0))
        except (TypeError, ValueError):
            dist_epoch = "0"
        dist_evr = (dist_epoch, dist_ver, dist_rel or "0")
        repo_evrs = self._source_repo_evrs(n)
        if not repo_evrs:
            return not require_evr
        best = repo_evrs[0]
        for evr in repo_evrs[1:]:
            if rpm_evr_compare(evr, best) > 0:
                best = evr
        return rpm_evr_compare(best, dist_evr) >= 0


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


def _add_capability_evr(
    index: RepoCapabilityIndex,
    name: str,
    epoch: str | None,
    ver: str | None,
    rel: str | None,
) -> None:
    n = (name or "").strip()
    v = (ver or "").strip()
    if not n or not v:
        return
    evr = ((epoch or "0").strip() or "0", v, (rel or "").strip())
    bucket = index.capability_evrs.setdefault(n, [])
    if evr not in bucket:
        bucket.append(evr)


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
        ver_node = pkg.find("{*}version")
        pkg_epoch = (ver_node.attrib.get("epoch") if ver_node is not None else None) or "0"
        pkg_ver = (ver_node.attrib.get("ver") if ver_node is not None else None) or ""
        pkg_rel = (ver_node.attrib.get("rel") if ver_node is not None else None) or ""
        if name:
            index.binary_names.add(name)
            index.provides.add(name)
            index.capability_providers.setdefault(name, set()).add(name)
            _add_capability_evr(index, name, pkg_epoch, pkg_ver, pkg_rel)
        sourcerpm = (pkg.findtext(".//{*}sourcerpm") or "").strip()
        if sourcerpm.endswith(".src.rpm") and name:
            src = sourcerpm[:-8].rsplit("-", 2)[0]
            if src:
                index.source_names.add(src)
                index.binary_source[name] = src
        for entry in pkg.findall(".//{*}provides/{*}entry"):
            prov = (entry.attrib.get("name") or "").strip()
            if not prov:
                continue
            index.provides.add(prov)
            if name:
                index.capability_providers.setdefault(prov, set()).add(name)
            _add_capability_evr(
                index,
                prov,
                entry.attrib.get("epoch") or pkg_epoch,
                entry.attrib.get("ver") or pkg_ver,
                entry.attrib.get("rel") or "",
            )
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


def _cap_cache_root() -> Path:
    raw = (os.environ.get("OREON_CAP_CACHE_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".oreon" / "cap-cache"


def _meta_blob_path(releasename: str, branch: str, arch: str, kind: str, digest: str) -> Path:
    return _cap_cache_root() / releasename / branch / arch / f"{digest}.{kind}.bin"


def _read_blob(path: Path) -> bytes | None:
    try:
        if path.is_file() and path.stat().st_size > 32:
            return path.read_bytes()
    except Exception:
        logger.debug("cap blob read failed %s", path, exc_info=True)
    return None


def _write_blob(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except Exception:
        logger.debug("cap blob write failed %s", path, exc_info=True)


def _fetch_repodata_blob(
    r2: Any,
    key: str | None,
    *,
    cache_path: Path,
) -> bytes | None:
    if not key:
        return None
    cached = _read_blob(cache_path)
    if cached is not None:
        return cached
    raw = r2.get_object(key)
    if not raw:
        return None
    _write_blob(cache_path, raw)
    return raw


def _load_arch_metadata(
    releasename: str,
    branch: str,
    arch: str,
    *,
    include_filelists: bool | None = None,
) -> RepoCapabilityIndex:
    partial = RepoCapabilityIndex()
    r2 = get_r2_client()
    repo_prefix = f"{releasename}/{branch}/{arch}"
    repomd = r2.get_object(f"{repo_prefix}/repodata/repomd.xml")
    if not repomd:
        return partial

    digest = hashlib.sha256(repomd).hexdigest()[:24]
    primary_key = _resolve_repodata_key(repo_prefix, repomd, "primary")
    primary_raw = _fetch_repodata_blob(
        r2,
        primary_key,
        cache_path=_meta_blob_path(releasename, branch, arch, "primary", digest),
    )
    if primary_raw and primary_key:
        _ingest_primary_xml(partial, _decompress_if_needed(primary_raw, primary_key))

    want_fl = _INCLUDE_FILELISTS if include_filelists is None else include_filelists
    if want_fl:
        filelists_key = _resolve_repodata_key(repo_prefix, repomd, "filelists")
        filelists_raw = _fetch_repodata_blob(
            r2,
            filelists_key,
            cache_path=_meta_blob_path(releasename, branch, arch, "filelists", digest),
        )
        if filelists_raw and filelists_key:
            _ingest_filelists_xml(partial, _decompress_if_needed(filelists_raw, filelists_key))

    return partial


def _load_arch_primary(releasename: str, branch: str, arch: str) -> RepoCapabilityIndex:
    return _load_arch_metadata(releasename, branch, arch, include_filelists=False)


def _copy_index_fields(dst: RepoCapabilityIndex, src: RepoCapabilityIndex) -> None:
    dst.binary_names.update(src.binary_names)
    dst.provides.update(src.provides)
    dst.source_names.update(src.source_names)
    dst.binary_names_any.update(src.binary_names_any or src.binary_names)
    dst.source_names_any.update(src.source_names_any or src.source_names)
    for cap, pkgs in src.capability_providers.items():
        dst.capability_providers.setdefault(cap, set()).update(pkgs)
    for cap, evrs in src.capability_evrs.items():
        bucket = dst.capability_evrs.setdefault(cap, [])
        for evr in evrs:
            if evr not in bucket:
                bucket.append(evr)
    for pkg, reqs in src.binary_requires.items():
        dst.binary_requires.setdefault(pkg, set()).update(reqs)
    dst.binary_source.update(src.binary_source)


def _best_evr(evrs: list[tuple[str, str, str]]) -> tuple[str, str, str] | None:
    if not evrs:
        return None
    best = evrs[0]
    for evr in evrs[1:]:
        if rpm_evr_compare(evr, best) > 0:
            best = evr
    return best


def _worst_evr(evrs: list[tuple[str, str, str]]) -> tuple[str, str, str] | None:
    if not evrs:
        return None
    worst = evrs[0]
    for evr in evrs[1:]:
        if rpm_evr_compare(evr, worst) < 0:
            worst = evr
    return worst


def _union_indexes(parts: list[RepoCapabilityIndex]) -> RepoCapabilityIndex:
    index = RepoCapabilityIndex()
    for part in parts:
        _copy_index_fields(index, part)
    return index


def _merge_arch_indexes(parts: list[RepoCapabilityIndex]) -> RepoCapabilityIndex:
    index = RepoCapabilityIndex()
    if not parts:
        index.finalize_lookups()
        return index
    if len(parts) == 1:
        _copy_index_fields(index, parts[0])
        if not index.source_names_any:
            index.source_names_any = set(index.source_names)
        if not index.binary_names_any:
            index.binary_names_any = set(index.binary_names)
        index.finalize_lookups()
        return index

    common_bins = set(parts[0].binary_names)
    common_provides = set(parts[0].provides)
    common_sources = set(parts[0].source_names)
    any_bins: set[str] = set()
    any_sources: set[str] = set()
    for part in parts:
        any_bins |= part.binary_names
        any_sources |= part.source_names
    for part in parts[1:]:
        common_bins &= part.binary_names
        common_provides &= part.provides
        common_sources &= part.source_names
    index.binary_names = common_bins
    index.provides = common_provides
    index.source_names = common_sources
    index.binary_names_any = any_bins
    index.source_names_any = any_sources
    for part in parts:
        for cap, pkgs in part.capability_providers.items():
            index.capability_providers.setdefault(cap, set()).update(pkgs)
        for pkg, reqs in part.binary_requires.items():
            index.binary_requires.setdefault(pkg, set()).update(reqs)
        index.binary_source.update(part.binary_source)
    # multi-arch version floor: best EVR on each arch, then weakest arch wins
    for cap in common_provides:
        per_arch_best: list[tuple[str, str, str]] = []
        for part in parts:
            best = _best_evr(list(part.capability_evrs.get(cap) or []))
            if best is not None:
                per_arch_best.append(best)
        floor = _worst_evr(per_arch_best)
        if floor is not None:
            index.capability_evrs[cap] = [floor]
    index.finalize_lookups()
    return index


def load_repo_capability_index(
    releasename: str,
    branch: str,
    architectures: list[str],
    *,
    extra_branches: list[str] | None = None,
) -> RepoCapabilityIndex:
    """
    Multi-arch index. A capability is only "satisfied" if every target arch has it.
    Unioning arches was wrong: aarch64-only dbus-c++ made x86_64 plans skip the BR.

    extra_branches are unioned per-arch with the publish branch. Mock often installs
    from testing while builds still publish into dest, so planning must see both.
    """
    rel = (releasename or "").strip()
    branches: list[str] = []
    for raw in [(branch or "dev").strip(), *((extra_branches or []))]:
        b = (raw or "").strip()
        if b and b not in branches:
            branches.append(b)
    if not branches:
        branches = ["dev"]
    arches = sorted({(a or "").strip() for a in architectures if (a or "").strip()})
    arch_key = ",".join(arches)
    branch_key = "+".join(branches)
    cache_key = (rel, branch_key, arch_key)

    with _cache_lock:
        now = time.monotonic()
        for k, (ts, _idx) in list(_cache.items()):
            if now - ts >= _CACHE_TTL_SEC:
                _cache.pop(k, None)
        hit = _cache.get(cache_key)
        if hit and now - hit[0] < _CACHE_TTL_SEC:
            _cache[cache_key] = (now, hit[1])
            return hit[1]

        while len(_cache) >= _CACHE_MAX:
            oldest_k = min(_cache.items(), key=lambda kv: kv[1][0])[0]
            _cache.pop(oldest_k, None)

        parts: list[RepoCapabilityIndex] = []
        for a in arches:
            per_branch = [
                _load_arch_metadata(rel, b, a, include_filelists=_INCLUDE_FILELISTS)
                for b in branches
            ]
            parts.append(
                _union_indexes(per_branch)
                if len(per_branch) > 1
                else (per_branch[0] if per_branch else RepoCapabilityIndex())
            )
        index = _merge_arch_indexes(parts)
        _cache[cache_key] = (now, index)
        logger.info(
            "repo capability index %s/%s arches=%s sources=%s binaries=%s provides=%s filelists=%s",
            rel,
            branch_key,
            arch_key or "(none)",
            len(index.source_names),
            len(index.binary_names),
            len(index.provides),
            _INCLUDE_FILELISTS,
        )
        return index


def clear_repo_capability_cache() -> None:
    with _cache_lock:
        _cache.clear()
    import gc

    gc.collect()
