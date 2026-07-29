"""Map virtual RPM caps to distgit source package names.

Discovery only. Never used as installability proof.
"""

from __future__ import annotations

import re
from typing import Iterable

_RE_PKGCONFIG = re.compile(r"^pkgconfig\((.+)\)$", re.IGNORECASE)
_RE_PYTHON3DIST = re.compile(r"^python3(?:\.\d+)?dist\((.+)\)$", re.IGNORECASE)
_RE_TEX = re.compile(r"^tex\((.+)\)$", re.IGNORECASE)
_RE_MVN = re.compile(r"^mvn\(([^)]+)\)$", re.IGNORECASE)
_RE_MINGW_DLL = re.compile(
    r"^mingw(32|64)\((lib[A-Za-z0-9_+.-]+\.dll)\)$", re.IGNORECASE
)
_RE_GHC_DEVEL = re.compile(r"^ghc-devel\((.+)\)$", re.IGNORECASE)
_RE_MONO = re.compile(r"^mono\((.+)\)$", re.IGNORECASE)
_RE_PC_VER = re.compile(r"^(.*?)-(\d+(?:\.\d+)*)$")
_RE_GHC_INNER = re.compile(r"^(.+)-(\d[\w.]*)-([A-Za-z0-9]+)$")

# mingw DLL stem → mingw source leaf (mingw-<leaf> / mingw32-<leaf>)
_MINGW_LIB_TO_SRC = {
    "libintl": "gettext",
    "libgmp": "gmp",
    "libtasn1": "libtasn1",
    "libgcc_s_seh": "gcc",
    "libgcc_s_dw2": "gcc",
    "libstdc++": "gcc",
    "libwinpthread": "winpthreads",
    "libssp": "gcc",
    "libffi": "libffi",
    "libiconv": "win-iconv",
    "zlib1": "zlib",
    "libz": "zlib",
    "libssl": "openssl",
    "libcrypto": "openssl",
}


def _hit(known: set[str], *cands: str) -> set[str]:
    out: set[str] = set()
    for c in cands:
        n = (c or "").strip()
        if n and n in known:
            out.add(n)
    return out


def font_binary_to_sources(name: str, package_names: set[str]) -> set[str]:
    """liberation-mono-fonts → liberation-fonts; amiri-quran-fonts → amiri-fonts."""
    n = (name or "").strip()
    known = package_names or set()
    if not n:
        return set()
    if n in known:
        return {n}
    if not n.endswith("-fonts"):
        return set()
    base = n[: -len("-fonts")]
    parts = [p for p in base.split("-") if p]
    out: set[str] = set()
    for i in range(len(parts) - 1, 0, -1):
        cand = "-".join(parts[:i]) + "-fonts"
        if cand in known:
            out.add(cand)
    return out


def pkgconfig_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    m = _RE_PKGCONFIG.match((cap or "").strip())
    if not m:
        return set()
    inner = (m.group(1) or "").strip()
    if not inner or "%" in inner:
        return set()
    known = package_names or set()
    out: set[str] = set()

    def add_from_map(key: str) -> None:
        for src in provider_map.get(key, set()) | provider_map.get(key.lower(), set()):
            if src:
                out.add(src)
        lower = provider_map.get("__lower__")
        if isinstance(lower, dict):
            for src in lower.get(key.lower(), set()):
                if src:
                    out.add(src)

    add_from_map(f"pkgconfig({inner})")
    # liborcus-0.20 → liborcus-0.21 may exist; also strip version
    base = inner
    vm = _RE_PC_VER.match(inner)
    if vm:
        base = vm.group(1)
        add_from_map(f"pkgconfig({base})")
        # any pkgconfig(base-*) in map
        prefix = f"pkgconfig({base}-"
        for key, srcs in provider_map.items():
            if key == "__lower__" or not isinstance(srcs, (set, frozenset, list, tuple)):
                continue
            if key.startswith(prefix) and key.endswith(")"):
                out.update(s for s in srcs if s)
    # name heuristics
    out |= _hit(
        known,
        base,
        f"{base}-devel",
        base[3:] if base.startswith("lib") else "",
        f"{base[3:]}-devel" if base.startswith("lib") else "",
    )
    # tcmalloc lives in gperftools
    if "tcmalloc" in inner.lower() or "profiler" in inner.lower():
        out |= _hit(known, "gperftools", "gperftools-devel")
    return {s for s in out if s}


def python3dist_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    m = _RE_PYTHON3DIST.match((cap or "").strip().split(" ", 1)[0])
    if not m:
        return set()
    inner = (m.group(1) or "").strip()
    if not inner:
        return set()
    known = package_names or set()
    out: set[str] = set()
    variants = {
        inner,
        inner.lower(),
        inner.replace("_", "-"),
        inner.replace("-", "_"),
        inner.lower().replace("_", "-"),
        inner.lower().replace("-", "_"),
    }
    for v in variants:
        key = f"python3dist({v})"
        for src in provider_map.get(key, set()) | provider_map.get(key.lower(), set()):
            if src:
                out.add(src)
        lower = provider_map.get("__lower__")
        if isinstance(lower, dict):
            for src in lower.get(key.lower(), set()):
                if src:
                    out.add(src)
        out |= _hit(
            known,
            f"python-{v}",
            f"python3-{v}",
            f"python-{v.lower()}",
            f"python3-{v.lower()}",
        )
    # case-insensitive scan of provider map keys
    want = {f"python3dist({v})".lower() for v in variants}
    for key, srcs in provider_map.items():
        if key == "__lower__" or not isinstance(srcs, (set, frozenset, list, tuple)):
            continue
        if key.lower() in want:
            out.update(s for s in srcs if s)
    return {s for s in out if s}


def tex_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    m = _RE_TEX.match((cap or "").strip())
    if not m:
        return set()
    fname = (m.group(1) or "").strip()
    if not fname:
        return set()
    known = package_names or set()
    out: set[str] = set()
    for key in (f"tex({fname})", cap):
        out |= _pmap_sources(provider_map, key)
    stem = fname.rsplit(".", 1)[0] if "." in fname else fname
    stem = stem.strip()
    leaf = f"texlive-{stem}"
    leaf_l = f"texlive-{stem.lower()}"
    out |= _hit(known, leaf, leaf_l)
    # binary subpackages from mega specs (%package -n texlive-a4wide)
    out |= _pmap_sources(provider_map, leaf, leaf_l)
    return {s for s in out if s}


def tex_import_suggestion(cap: str) -> str | None:
    m = _RE_TEX.match((cap or "").strip())
    if not m:
        return None
    fname = (m.group(1) or "").strip()
    if not fname:
        return None
    stem = fname.rsplit(".", 1)[0] if "." in fname else fname
    return f"texlive-{stem}" if stem else None


def _mvn_group_artifact(inner: str) -> tuple[str, str]:
    """Parse mvn() coords. group:artifact or group:artifact:version[:...]."""
    parts = [p.strip() for p in (inner or "").split(":") if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], parts[1]


def mvn_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    m = _RE_MVN.match((cap or "").strip())
    if not m:
        return set()
    ga = (m.group(1) or "").strip()
    if not ga or "%" in ga:
        return set()
    known = package_names or set()
    out: set[str] = set()
    for key in (f"mvn({ga})", cap):
        out |= _pmap_sources(provider_map, key)
    # also match unversioned mvn(group:artifact) Provides
    group, artifact = _mvn_group_artifact(ga)
    if group and artifact:
        out |= _pmap_sources(provider_map, f"mvn({group}:{artifact})")
    if not artifact:
        return {s for s in out if s}
    cands: list[str] = [artifact, artifact.replace(".", "-"), artifact.replace(".", "_")]
    if artifact.endswith("-plugin"):
        cands.append(artifact[: -len("-plugin")])
        cands.append(artifact.replace("-plugin", ""))
    if artifact.startswith("javax."):
        rest = artifact[len("javax.") :].replace(".", "-")
        cands += [f"jakarta-{rest}", f"jakarta-{rest}1.0", rest]
    if group.startswith("javax."):
        rest = group[len("javax.") :].replace(".", "-")
        cands += [f"jakarta-{rest}", f"jakarta-{rest}1.0"]
    if "surefire" in artifact:
        cands += ["maven-surefire", "maven-surefire-plugin"]
    out |= _hit(known, *cands)
    return {s for s in out if s}


def mingw_dll_to_sources(cap: str, package_names: set[str]) -> set[str]:
    m = _RE_MINGW_DLL.match((cap or "").strip())
    if not m:
        return set()
    known = package_names or set()
    arch = m.group(1)
    dll = m.group(2)
    stem = dll[:-4] if dll.lower().endswith(".dll") else dll
    stem = re.sub(r"-\d+$", "", stem)
    leaf = _MINGW_LIB_TO_SRC.get(stem.lower())
    if not leaf and stem.lower().startswith("lib"):
        leaf = _MINGW_LIB_TO_SRC.get(stem.lower()) or stem[3:]
    if not leaf:
        leaf = stem[3:] if stem.lower().startswith("lib") else stem
    return _hit(
        known,
        f"mingw-{leaf}",
        f"mingw{arch}-{leaf}",
        f"mingw-{stem}",
        f"mingw{arch}-{stem}",
    )


def ghc_devel_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    m = _RE_GHC_DEVEL.match((cap or "").strip())
    if not m:
        return set()
    inner = (m.group(1) or "").strip()
    if not inner:
        return set()
    known = package_names or set()
    out: set[str] = set()
    for key in (f"ghc-devel({inner})", cap):
        for src in provider_map.get(key, set()) | provider_map.get(key.lower(), set()):
            if src:
                out.add(src)
    name = inner
    hm = _RE_GHC_INNER.match(inner)
    if hm:
        name = hm.group(1)
    out |= _hit(
        known,
        f"ghc-{name}",
        f"ghc-{name}-devel",
        f"ghc-{name.replace('_', '-')}",
        f"ghc-{name.replace('_', '-')}-devel",
    )
    return {s for s in out if s}


def _pmap_sources(provider_map: dict, *keys: str) -> set[str]:
    out: set[str] = set()
    for key in keys:
        raw = provider_map.get(key) or provider_map.get((key or "").lower()) or ()
        if isinstance(raw, (set, frozenset)):
            out.update(s for s in raw if s)
        elif isinstance(raw, (list, tuple)):
            out.update(s for s in raw if s)
    return out


def mono_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    m = _RE_MONO.match((cap or "").strip())
    if not m:
        return set()
    known = package_names or set()
    out = _pmap_sources(provider_map, cap, cap.lower())
    if "mono" in known:
        out.add("mono")
    return {s for s in out if s}


def devel_subpkg_to_sources(cap: str, package_names: set[str]) -> set[str]:
    n = (cap or "").strip()
    known = package_names or set()
    if not n or n in known:
        return _hit(known, n) if n else set()
    if not n.endswith("-devel"):
        return set()
    base = n[: -len("-devel")]
    return _hit(known, base)


def distgit_sources_for_virtual_cap(
    requirement: str,
    *,
    package_names: set[str],
    provider_map: dict | None = None,
) -> set[str]:
    raw = (requirement or "").strip()
    if not raw:
        return set()
    # drop version ops for matching
    primary = re.split(r"\s*(?:<=|>=|==|!=|<|>|=)\s*", raw, maxsplit=1)[0].strip()
    known = package_names or set()
    pmap = provider_map or {}
    out: set[str] = set()

    if primary in known:
        out.add(primary)
    out |= font_binary_to_sources(primary, known)
    out |= pkgconfig_to_sources(primary, known, pmap)
    out |= python3dist_to_sources(primary, known, pmap)
    out |= tex_to_sources(primary, known, pmap)
    out |= mvn_to_sources(primary, known, pmap)
    out |= mingw_dll_to_sources(primary, known)
    out |= ghc_devel_to_sources(primary, known, pmap)
    out |= mono_to_sources(primary, known, pmap)
    out |= devel_subpkg_to_sources(primary, known)
    return {s for s in out if s}


def import_suggestion_for_cap(requirement: str) -> str | None:
    """When nothing is in distgit, prefer a package-shaped import name over a virtual cap."""
    from oreon_build.core.rpm_requires import is_spurious_solver_miss

    raw = (requirement or "").strip()
    primary = re.split(r"\s*(?:<=|>=|==|!=|<|>|=)\s*", raw, maxsplit=1)[0].strip()
    tex = tex_import_suggestion(primary)
    if tex:
        return tex
    m = _RE_GHC_DEVEL.match(primary)
    if m:
        inner = m.group(1)
        hm = _RE_GHC_INNER.match(inner)
        name = hm.group(1) if hm else inner
        return f"ghc-{name}"
    m = _RE_MVN.match(primary)
    if m:
        _group, artifact = _mvn_group_artifact(m.group(1))
        if not artifact or is_spurious_solver_miss(artifact):
            return None
        if artifact.endswith("-plugin"):
            return artifact[: -len("-plugin")] if artifact[: -len("-plugin")] else artifact
        if artifact.startswith("javax."):
            return "jakarta-" + artifact[len("javax.") :].replace(".", "-")
        # keep dotted maven artifacts as rpm-ish only when not a bare version
        cand = artifact.replace(".", "-")
        if is_spurious_solver_miss(cand):
            return None
        return cand
    if primary.endswith("-devel"):
        base = primary[: -len("-devel")]
        if base and not is_spurious_solver_miss(base):
            return base
    return None
