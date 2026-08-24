"""Provider map only: capability → distgit source names. Never installability."""

from __future__ import annotations

import re
from typing import Any

from oreon_build.core.rpm_requires import (
    evr_satisfies,
    is_distgit_source_name,
    merge_provider_maps,
    requirement_check_capabilities,
    sources_providing_requirement,
    split_requirement_evr,
)
from oreon_build.services.dep_plan.br_filter import filter_plan_brs
from oreon_build.services.repo_capability_index import is_rich_dependency, is_rpmlib_requirement

__all__ = [
    "BASE_SYSTEM_PACKAGES",
    "actionable_caps",
    "filter_plan_brs",
    "filter_providers_for_consumer",
    "is_base_system_package",
    "is_mingw_requirement",
    "is_mingw_source",
    "is_toolchainish_source",
    "merge_maps",
    "narrow_alternate_providers",
    "providers_for_requirement",
    "should_seed_providers",
]

_RE_CRATE_VER_LEAF = re.compile(
    r"crate\((?P<inner>[^)]+)\)\s*(?P<op>>=|<=|>|<|=|==)\s*(?P<ver>[^\s,)]+)"
)
_RE_GHC_ALT = re.compile(r"^ghc\d", re.IGNORECASE)
_RE_VERSION_SUFFIX = re.compile(r"\d")


# Packages assumed present in the build environment (buildsys-build group).
# These are fundamental build tools that should never be pulled in from
# distgit source as a chain-plan build dependency.
BASE_SYSTEM_PACKAGES: frozenset[str] = frozenset(
    {
        "bash",
        "binutils",
        "bison",
        "bzip2",
        "coreutils",
        "cpio",
        "diffutils",
        "expat",
        "file",
        "findutils",
        "gawk",
        "gcc",
        "gcc-c++",
        "gdb",
        "gettext",
        "glibc",
        "glibc-common",
        "glibc-devel",
        "glibc-minimal-langpacks",
        "gmp",
        "grep",
        "groff-base",
        "gzip",
        "info",
        "iptables",
        "libacl",
        "libattr",
        "libcap",
        "libffi",
        "libgcc",
        "libgomp",
        "libselinux",
        "libsepol",
        "libstdc++",
        "libstdc++-devel",
        "libtool",
        "libxcrypt",
        "make",
        "patch",
        "perl",
        "pkgconf",
        "pkgconf-m4",
        "pkgconf-pkg-config",
        "redhat-rpm-config",
        "rpm",
        "rpm-build",
        "rpm-build-libs",
        "sed",
        "setup",
        "shadow-utils",
        "tar",
        "tbb",
        "unzip",
        "util-linux",
        "which",
        "xz",
        "zlib",
        "binutils-devel",
    }
)


def is_base_system_package(name: str) -> bool:
    """True when *name* is a base-system build tool assumed present in mock."""
    n = (name or "").strip().lower()
    return n in BASE_SYSTEM_PACKAGES


_RE_MINGW_REQ = re.compile(
    r"(?i)(?:^|[\s,(])mingw(?:32|64)(?:-|\()|^mingw(?:32|64)-"
)


def is_mingw_source(name: str) -> bool:
    n = (name or "").strip().lower()
    return n.startswith("mingw-") or n.startswith("mingw32-") or n.startswith("mingw64-")


def is_mingw_requirement(req: str) -> bool:
    """True for mingw32/mingw64 package or dll provides."""
    r = (req or "").strip()
    if not r:
        return False
    low = r.lower()
    if low.startswith("mingw32-") or low.startswith("mingw64-") or low.startswith("mingw-"):
        return True
    return bool(_RE_MINGW_REQ.search(r))


def filter_providers_for_consumer(consumer: str, srcs: set[str]) -> set[str]:
    """Prefer same-family providers. Native never picks mingw/haskell when a native exists."""
    out = {s for s in srcs if s}
    if not out:
        return out
    if is_mingw_source(consumer):
        mingw = {s for s in out if is_mingw_source(s)}
        return mingw or out
    if is_toolchainish_source(consumer):
        tools = {s for s in out if is_toolchainish_source(s)}
        return tools or out
    preferred = {
        s for s in out if not is_mingw_source(s) and not is_toolchainish_source(s)
    }
    if preferred:
        return preferred
    native = {s for s in out if not is_mingw_source(s)}
    return native or out


def _is_ghc_alternate_compiler(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return False
    if n == "ghc":
        return True
    return bool(_RE_GHC_ALT.match(n))


def is_toolchainish_source(name: str) -> bool:
    """ghc alternate-compiler namespace (ghc, ghcNN, ghc-*). Prefer any-arch repo presence."""
    n = (name or "").strip()
    if not n:
        return False
    if _is_ghc_alternate_compiler(n):
        return True
    return n.lower().startswith("ghc-")


def is_ghc_requirement(req: str) -> bool:
    """The cap itself sits in the ghc namespace, so a ghc source is its real owner.

    ghc ships base/Cabal/bytestring as subpackages. A package asking for those by
    name wants the haskell stack, whoever it is, so pulling ghc is the answer and
    not a foreign stack leaking into a native graph.
    """
    raw = (req or "").strip()
    if not raw:
        return False
    from oreon_build.core.rpm_requires import requirement_check_capabilities

    for cap in requirement_check_capabilities(raw) or [raw]:
        c = (cap or "").strip()
        if not c:
            continue
        if c.startswith("libHS"):
            return True
        low = c.lower()
        if low == "ghc" or low.startswith("ghc-"):
            return True
    return False


def should_seed_providers(consumer: str, req: str, srcs: set[str]) -> bool:
    """False when seed must not pull foreign compiler/cross stacks into a native graph.

    Also False when the only providers are base-system build tools (gcc, make, etc.)
    that are assumed present in the build environment.
    """
    if not srcs:
        return False
    if is_mingw_requirement(req) and not is_mingw_source(consumer):
        return False
    if (
        all(is_toolchainish_source(s) for s in srcs)
        and not is_toolchainish_source(consumer)
        and not is_ghc_requirement(req)
    ):
        return False
    consumer_low = (consumer or "").strip().lower()
    if consumer_low and is_base_system_package(consumer_low):
        return True
    if all(is_base_system_package(s) for s in srcs):
        req_name = _requirement_name(req)
        if req_name and is_base_system_package(req_name):
            return False
    return True


def _requirement_name(req: str) -> str | None:
    """Extract the bare capability name from a requirement like 'gcc >= 13'."""
    from oreon_build.core.rpm_requires import parse_versioned_requirement

    name, _op, _ver = parse_versioned_requirement((req or "").strip())
    if name:
        return name.strip()
    cap = (req or "").strip()
    if cap.startswith("(") and cap.endswith(")"):
        return None
    return cap or None


def narrow_alternate_providers(requirement: str, srcs: set[str]) -> set[str]:
    """Collapse overloaded alternate-compiler sets (ghc + ghc9.*) to the primary."""
    out = {s for s in srcs if s}
    if len(out) <= 1:
        return out
    ghc_alts = {s for s in out if _is_ghc_alternate_compiler(s)}
    if len(ghc_alts) >= 2 and ghc_alts == out:
        if "ghc" in ghc_alts:
            return {"ghc"}
        return {sorted(ghc_alts, key=lambda s: (len(s), s))[0]}
    if len(ghc_alts) >= 2 and "ghc" in ghc_alts:
        return (out - ghc_alts) | {"ghc"}
    primary = (requirement or "").strip()
    if primary and primary in out:
        # cap name equals a source and other hits look like versioned alts of it
        alts = {
            s
            for s in out
            if s != primary and s.startswith(primary) and _RE_VERSION_SUFFIX.search(s[len(primary) :])
        }
        if alts and alts | {primary} == out:
            return {primary}
    return out


def merge_maps(*maps: dict[str, set[str]] | None) -> dict[str, set[str]]:
    return merge_provider_maps(*[m for m in maps if m])


_CASEFOLD_INDEX_CACHE: dict[int, dict[str, set[str]]] = {}


def _build_casefolded_index(provider_map: dict) -> dict[str, set[str]]:
    if not provider_map:
        return {}
    cached = _CASEFOLD_INDEX_CACHE.get(id(provider_map))
    if cached is not None:
        return cached
    lower_key = provider_map.get("__lower__")
    if isinstance(lower_key, dict):
        idx = lower_key
    else:
        idx = {}
        for key, sources in provider_map.items():
            if not isinstance(key, str) or key == "__lower__":
                continue
            folded = key.casefold()
            bucket = idx.get(folded)
            if bucket is None:
                bucket = set()
                idx[folded] = bucket
            if isinstance(sources, (set, frozenset)):
                bucket.update(s for s in sources if s)
            elif isinstance(sources, (list, tuple)):
                bucket.update(s for s in sources if s)
    _CASEFOLD_INDEX_CACHE[id(provider_map)] = idx
    return idx


def _map_sources(provider_map: dict, cap: str) -> set[str]:
    from oreon_build.services.virtual_resolve import _pmap_sources

    result = _pmap_sources(provider_map, cap, (cap or "").lower())
    if result:
        return result
    folded = (cap or "").casefold()
    if not folded:
        return result
    idx = _build_casefolded_index(provider_map)
    if idx:
        cached = idx.get(folded)
        if cached:
            result.update(cached)
    return result


def _rust_compat_series_from_package(pkg: str) -> str | None:
    """rust-rustix0.37 → 0.37, rust-nom7 → 7, rust-rustix → None."""
    from oreon_build.services.distgit_order import _rust_crate_name_from_package

    n = (pkg or "").strip()
    crate = _rust_crate_name_from_package(n)
    if not crate or not n.startswith("rust-"):
        return None
    rest = n[5:]
    if not rest.startswith(crate):
        return None
    suffix = rest[len(crate) :]
    if re.fullmatch(r"0\.\d+(?:\.\d+)*", suffix) or re.fullmatch(r"\d+", suffix):
        return suffix
    return None


def _crate_version_constraints(requirement: str) -> list[tuple[str, str, str]]:
    """(crate_name, op, ver) leaves from rich/plain crate Requires."""
    raw = (requirement or "").strip()
    if not raw or "crate(" not in raw:
        return []
    out: list[tuple[str, str, str]] = []
    for m in _RE_CRATE_VER_LEAF.finditer(raw):
        inner = (m.group("inner") or "").strip()
        op = (m.group("op") or "").strip()
        ver = (m.group("ver") or "").strip()
        if not inner or not op or not ver or "%" in ver:
            continue
        crate = inner.split("/", 1)[0].strip()
        if not crate:
            continue
        if op == "==":
            op = "="
        out.append((crate, op, ver))
    return out


def _series_as_provide_ver(series: str) -> str:
    """Compat dir 0.37 means 0.37.x; pad so rpmvercmp matches cargo bounds."""
    s = (series or "").strip()
    if not s:
        return "0"
    parts = s.split(".")
    if len(parts) == 2 and parts[0] == "0" and parts[1].isdigit():
        return f"{s}.0"
    if len(parts) == 1 and parts[0].isdigit():
        return s
    return s


def _series_next(series: str) -> str | None:
    """Next cargo-compat series boundary: 0.1 → 0.2, 7 → 8."""
    s = (series or "").strip()
    parts = s.split(".")
    if len(parts) == 2 and parts[0] == "0" and parts[1].isdigit():
        return f"0.{int(parts[1]) + 1}"
    if len(parts) == 1 and parts[0].isdigit():
        return str(int(parts[0]) + 1)
    return None


def _source_may_satisfy_crate_versions(src: str, requirement: str) -> bool:
    """False only when a compat-suffix package cannot meet crate version bounds.

    Compat dirs name a series range [series, next), not a single version.
    rust-structmeta0.1 can satisfy >= 0.1.4 with < 0.2 even though the
    series token pads to 0.1.0. Unsuffixed rust-foo is always kept.
    rust-similar2 is similar series 2, not a crate named similar2.
    """
    constraints = _crate_version_constraints(requirement)
    if not constraints:
        return True
    n = (src or "").strip()
    for crate, op, ver in constraints:
        prefix = f"rust-{crate}"
        if n == prefix:
            continue
        if not n.startswith(prefix):
            continue
        series = n[len(prefix) :]
        if not (
            re.fullmatch(r"0\.\d+(?:\.\d+)*", series) or re.fullmatch(r"\d+", series)
        ):
            continue
        nxt = _series_next(series)
        if nxt is None:
            continue
        floor = split_requirement_evr(_series_as_provide_ver(series))
        ceiling = split_requirement_evr(_series_as_provide_ver(nxt))
        need = split_requirement_evr(ver)
        if op == ">=":
            if not evr_satisfies("<", need, ceiling):
                return False
        elif op == ">":
            if not evr_satisfies("<", need, ceiling):
                return False
        elif op == "<":
            if not evr_satisfies("<", floor, need):
                return False
        elif op == "<=":
            if not (
                evr_satisfies("<", floor, need) or evr_satisfies("=", floor, need)
            ):
                return False
        elif op == "=":
            if evr_satisfies(">", floor, need):
                return False
            if not evr_satisfies("<", need, ceiling):
                return False
    return True


def providers_for_requirement(
    requirement: str,
    *,
    provider_map: dict[str, set[str]],
    repo_index: Any | None = None,
    package_names: set[str] | None = None,
) -> set[str]:
    """Sources that Provide this cap (or rich-dep leaves). Map lookup only."""
    from oreon_build.core.rpm_requires import is_chain_plan_requirement

    raw = (requirement or "").strip()
    if not raw or is_rpmlib_requirement(raw):
        return set()
    if raw.startswith("(") and raw.endswith(")"):
        leaves = [c for c in requirement_check_capabilities(raw) if is_chain_plan_requirement(c)]
        if not leaves:
            return set()
    elif not is_chain_plan_requirement(raw):
        return set()
    out: set[str] = set()
    known = package_names or set()
    if repo_index is not None:
        out |= {
            s
            for s in sources_providing_requirement(
                raw, repo_index, provider_maps=[provider_map], exact=True
            )
            if is_distgit_source_name(s)
        }
    else:
        for cap in requirement_check_capabilities(raw):
            if not is_chain_plan_requirement(cap):
                continue
            for src in _map_sources(provider_map, cap):
                if is_distgit_source_name(src):
                    out.add(src)
            lower = provider_map.get("__lower__")
            if isinstance(lower, dict):
                for src in _map_sources(lower, cap.lower()):
                    if is_distgit_source_name(src):
                        out.add(src)
    for cap in requirement_check_capabilities(raw):
        if not is_chain_plan_requirement(cap):
            continue
        if cap in known and is_distgit_source_name(cap):
            out.add(cap)
        for src in _map_sources(provider_map, cap):
            if is_distgit_source_name(src):
                out.add(src)

    from oreon_build.services.perl_resolve import distgit_sources_for_perl_module
    from oreon_build.core.rpm_requires import rpm_requirement_primary

    perl_mods: list[str] = []
    primary = rpm_requirement_primary(raw) or raw
    if primary and primary.startswith("perl(") and primary.endswith(")"):
        perl_mods.append(primary[5:-1])
    elif raw.startswith("(") and "perl(" in raw:
        for cap in requirement_check_capabilities(raw):
            m = re.match(r"^perl\(([^)]+)\)$", cap)
            if m:
                perl_mods.append(m.group(1))
    else:
        m = re.match(r"^perl\(([^)]+)\)$", raw)
        if m:
            perl_mods.append(m.group(1))
    for mod in perl_mods:
        out |= {
            s
            for s in distgit_sources_for_perl_module(mod, known)
            if is_distgit_source_name(s)
        }
    from oreon_build.services.virtual_resolve import distgit_sources_for_virtual_cap

    out |= {
        s
        for s in distgit_sources_for_virtual_cap(
            raw, package_names=known, provider_map=provider_map
        )
        if is_distgit_source_name(s)
    }
    if not out and is_distgit_source_name(raw):
        if raw in known:
            out.add(raw)
        else:
            mapped = _map_sources(provider_map, raw)
            if mapped:
                out |= {s for s in mapped if is_distgit_source_name(s)}
            elif repo_index is not None and (
                raw in getattr(repo_index, "source_names", ())
                or getattr(repo_index, "satisfies_source", lambda _n: False)(raw)
            ):
                out.add(raw)
    if "crate(" in raw and _crate_version_constraints(raw):
        out = {s for s in out if _source_may_satisfy_crate_versions(s, raw)}
    else:
        out = _prefer_current_crate_sources(raw, out)
    if perl_mods:
        out = _prefer_specific_perl_sources(out)
    return out


def _prefer_specific_perl_sources(srcs: set[str]) -> set[str]:
    """Drop the interpreter and shorter perl-* prefixes when a longer owner exists.

    perl.spec Provides: perl(CPAN::Meta::...) for dual-life noise. DNF still
    installs perl-CPAN-Meta-Requirements for that module.
    """
    perl_like = {s for s in srcs if s == "perl" or s.startswith("perl-")}
    if not perl_like:
        return srcs
    rest = srcs - perl_like
    if "perl" in perl_like and any(s.startswith("perl-") for s in perl_like):
        perl_like.discard("perl")
    drop: set[str] = set()
    for a in perl_like:
        for b in perl_like:
            if a != b and b.startswith(a + "-"):
                drop.add(a)
    return rest | (perl_like - drop)


def _crate_names_in_requirement(requirement: str) -> list[str]:
    raw = (requirement or "").strip()
    if "crate(" not in raw:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"crate\(([^)/]+)(?:/[^)]*)?\)", raw):
        crate = (m.group(1) or "").strip()
        if crate and crate not in seen:
            seen.add(crate)
            names.append(crate)
    return names


def _prefer_current_crate_sources(requirement: str, srcs: set[str]) -> set[str]:
    """Unversioned crate(foo) → rust-foo, not every rust-foo0.N compat dir."""
    if not srcs or not _crate_names_in_requirement(requirement):
        return srcs
    drop: set[str] = set()
    for crate in _crate_names_in_requirement(requirement):
        current = f"rust-{crate}"
        if current not in srcs:
            continue
        prefix = current
        for s in srcs:
            if s == prefix:
                continue
            if not s.startswith(prefix):
                continue
            series = s[len(prefix) :]
            if re.fullmatch(r"0\.\d+(?:\.\d+)*", series) or re.fullmatch(r"\d+", series):
                drop.add(s)
    return srcs - drop


def actionable_caps(requirement: str) -> list[str]:
    raw = (requirement or "").strip()
    if not raw or is_rpmlib_requirement(raw):
        return []
    if is_rich_dependency(raw):
        return [c for c in requirement_check_capabilities(raw) if c]
    return requirement_check_capabilities(raw) or [raw]
