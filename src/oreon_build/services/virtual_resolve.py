"""Map virtual RPM caps to distgit source package names.

Resolution is driven by provider data only. A cap is decomposed into rich-dep
leaves, normalized to its equivalent authoritative forms, then looked up in the
distgit provides map. On top of that sit decoders for capability namespaces that
encode their owner in a fixed format (ghc sonames, ghc-devel/prof, mvn coords,
go import paths, crate compat series, tex style files).

No alias tables, no fuzzy name matching, no guessing a source from a cap that
does not declare it. Guessing invents providers that do not exist and spawns
junk rebuilds.

Discovery only. Never used as installability proof.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Iterable

_RE_PKGCONFIG = re.compile(r"^pkgconfig\((.+)\)$", re.IGNORECASE)
_RE_PYTHON3DIST = re.compile(r"^python3(?:\.\d+)?dist\((.+)\)$", re.IGNORECASE)
_RE_TEX = re.compile(r"^tex\((.+)\)$", re.IGNORECASE)
_RE_MVN = re.compile(r"^mvn\(([^)]+)\)$", re.IGNORECASE)
_RE_GHC_DEVEL = re.compile(r"^ghc-devel\((.+)\)$", re.IGNORECASE)
_RE_GHC_PROF = re.compile(r"^ghc-prof\((.+)\)$", re.IGNORECASE)
_RE_MONO = re.compile(r"^mono\((.+)\)$", re.IGNORECASE)
_RE_CMAKE = re.compile(r"^cmake\((.+)\)$", re.IGNORECASE)
_RE_FONT = re.compile(r"^font\((.+)\)$", re.IGNORECASE)
_RE_PC_VER = re.compile(r"^(.*?)-(\d+(?:\.\d+)*)$")
_RE_GHC_INNER = re.compile(r"^(.+)-(\d[\w.]*)-([A-Za-z0-9]+)$")
_RE_PY_EXTRA = re.compile(r"\[([^\]]*)\]\s*$")
_RE_BIN_PLUS = re.compile(r"^(.+)\+([A-Za-z0-9_.+-]+)$")
_RE_CRATE = re.compile(r"^crate\(([^)/]+)(?:/[^)]+)?\)$")

_RE_GHC_LIBHS = re.compile(
    r"^libHS(.+)-(\d[\w.]*)-[A-Za-z0-9]+-ghc[\d.]+\.so(?:\(\)\(64bit\))?$"
)


def _hit(known: set[str], *cands: str) -> set[str]:
    out: set[str] = set()
    for c in cands:
        n = (c or "").strip()
        if n and n in known:
            out.add(n)
    return out


def font_binary_to_sources(name: str, package_names: set[str]) -> set[str]:
    """A font binary resolves only when it is itself a known source."""
    n = (name or "").strip()
    if not n:
        return set()
    return _hit(package_names or set(), n)


def font_cap_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    """font(foo) resolves off the declared font() provide, nothing else.

    Font cap tokens are squashed upstream names that do not track the rpm name,
    so substring matching them against every *-fonts package invents providers.
    """
    m = _RE_FONT.match((cap or "").strip())
    if not m:
        return set()
    inner = (m.group(1) or "").strip()
    if not inner or "%" in inner:
        return set()
    return _pmap_sources(provider_map, f"font({inner})", cap)


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
    # a .pc name carries its api version (liborcus-0.20), the packaged one may sit
    # at another revision, so match any declared pkgconfig(base-*) provide.
    base = inner
    vm = _RE_PC_VER.match(inner)
    if vm:
        base, ver = vm.group(1), vm.group(2)
        add_from_map(f"pkgconfig({base})")
        prefix = f"pkgconfig({base}-"
        for key, srcs in provider_map.items():
            if key == "__lower__" or not isinstance(srcs, (set, frozenset, list, tuple)):
                continue
            if key.startswith(prefix) and key.endswith(")"):
                out.update(s for s in srcs if s)
        # mozjs-140 ships as mozjs140, the version folds into the name
        major = ver.split(".", 1)[0]
        nodots = ver.replace(".", "")
        for concat in (f"{base}{ver}", f"{base}{major}", f"{base}{nodots}"):
            add_from_map(f"pkgconfig({concat})")
            out |= _hit(known, concat, f"{concat}-devel")
    out |= _hit(known, inner, base)
    return {s for s in out if s}


def python3dist_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    m = _RE_PYTHON3DIST.match((cap or "").strip().split(" ", 1)[0])
    if not m:
        return set()
    inner = (m.group(1) or "").strip()
    if not inner:
        return set()
    # dask[array] / versioneer[toml] → base dist name
    inner = _RE_PY_EXTRA.sub("", inner).strip()
    if not inner:
        return set()
    known = package_names or set()
    out: set[str] = set()
    # pep503 treats -/_/case as the same dist, so these are equivalent spellings
    # of one name, not guesses at a different package.
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
        py_hits = _hit(
            known,
            f"python-{v}",
            f"python3-{v}",
            f"python-{v.lower()}",
            f"python3-{v.lower()}",
        )
        out |= py_hits
        # some pypi packages ship under their dist name with no python- prefix
        if not py_hits:
            out |= _hit(known, v, v.lower())
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
    # the artifact id is the rpm name for jpackage-style specs, dots become dashes
    cands = [artifact, artifact.replace(".", "-"), artifact.replace(".", "_")]
    out |= _hit(known, *cands)
    return {s for s in out if s}


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
    hyphen = name.replace("_", "-")
    dedicated = [f"ghc-{name}", f"ghc-{hyphen}"]
    for d in dedicated:
        if d in known:
            return {d}
    out |= _hit(
        known,
        f"ghc-{name}",
        f"ghc-{name}-devel",
        f"ghc-{hyphen}",
        f"ghc-{hyphen}-devel",
    )
    out |= _pmap_sources(
        provider_map,
        f"ghc-{name}",
        f"ghc-{name}-devel",
        f"ghc-{hyphen}",
        f"ghc-{hyphen}-devel",
        f"ghc-libhs({name})",
        f"ghc-libhs({hyphen})",
    )
    if not out:
        out |= _hit(known, "ghc")
    return {s for s in out if s}


def ghc_rpm_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    p = (cap or "").strip()
    if not p.lower().startswith("ghc-") or "(" in p:
        return set()
    known = package_names or set()
    if p.lower().endswith("-devel"):
        lib = p[: -len("-devel")]
        hit = _hit(known, lib, p)
        if hit:
            return hit
        mapped = _pmap_sources(provider_map, lib, p)
        if mapped:
            return mapped
        return _hit(known, "ghc")
    hit = _hit(known, p)
    if hit:
        return hit
    mapped = _pmap_sources(provider_map, p)
    if mapped:
        return mapped
    return _hit(known, "ghc")


def ghc_prof_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    """ghc-prof(crypton-1.0.5-Hash) / ghc-citeproc-prof → ghc-crypton / ghc-citeproc."""
    known = package_names or set()
    out: set[str] = set()
    primary = (cap or "").strip()
    m = _RE_GHC_PROF.match(primary)
    if m:
        inner = (m.group(1) or "").strip()
        name = inner
        hm = _RE_GHC_INNER.match(inner)
        if hm:
            name = hm.group(1)
        out |= _hit(
            known,
            f"ghc-{name}",
            f"ghc-{name.replace('_', '-')}",
        )
        out |= _pmap_sources(provider_map, f"ghc-prof({inner})", primary)
        return {s for s in out if s}
    if primary.startswith("ghc-") and primary.endswith("-prof"):
        base = primary[: -len("-prof")]
        out |= _hit(known, base, f"{base}-devel")
    return {s for s in out if s}


def cmake_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    m = _RE_CMAKE.match((cap or "").strip().split(" ", 1)[0])
    if not m:
        return set()
    inner = (m.group(1) or "").strip()
    if not inner or "%" in inner:
        return set()
    known = package_names or set()
    out = _pmap_sources(provider_map, f"cmake({inner})", cap)
    inner_key = inner.lower().removesuffix(".cmake")
    if inner_key in {"ecm", "ecmconfig", "findecm"}:
        out |= _pmap_sources(provider_map, "extra-cmake-modules", "cmake(ECM)")
        out |= _hit(known, "extra-cmake-modules")
    base = inner
    vm = _RE_PC_VER.match(inner)
    if vm:
        base = vm.group(1)
        out |= _pmap_sources(provider_map, f"cmake({base})")
    # the config module usually ships in a lib<base>/<base>-devel subpackage; walk
    # those provide/binary names back to their source through the provider map.
    out |= _pmap_sources(
        provider_map,
        base,
        f"lib{base}",
        f"{base}-devel",
        f"lib{base}-devel",
    )
    out |= _hit(
        known,
        base,
        f"{base}-devel",
        f"lib{base}",
        f"lib{base}-devel",
    )
    if inner.startswith("KF") and len(inner) > 3 and inner[2].isdigit():
        ver = inner[2]
        rest = inner[3:]
        low = rest.lower()
        kf_hits = [f"kf{ver}-k{low}", f"kf{ver}-{low}"]
        if rest.startswith("K") and len(rest) > 1:
            kf_hits.append(f"kf{ver}-{rest.lower()}")
            kf_hits.append(f"kf{ver}-k{rest[1:].lower()}")
        out |= _hit(known, *kf_hits)
    if inner.startswith("Qt") and len(inner) > 3 and inner[2].isdigit():
        ver = inner[2]
        rest = inner[3:]
        low = rest.lower()
        out |= _hit(known, f"qt{ver}-qt{low}", f"qt{ver}-{low}")
    # config modules often carry the soname version in the provide
    # (cmake(bson-2.3.1)) while consumers ask for the bare cmake(bson).
    if not out:
        out |= _cmake_module_provides(provider_map, base)
    return {s for s in out if s}


_RE_CMAKE_MODULE_VER_TAIL = re.compile(r"^-?\d[\w.]*$")


def _cmake_module_provides(provider_map: dict, base: str) -> set[str]:
    """Sources providing cmake(<base>), cmake(<base>-<ver>) or cmake(<base><ver>)."""
    low = (base or "").strip().lower()
    if not low:
        return set()
    out: set[str] = set()
    for key, srcs in (provider_map or {}).items():
        if not isinstance(key, str):
            continue
        kl = key.lower()
        if not kl.startswith("cmake(") or not kl.endswith(")"):
            continue
        module = kl[len("cmake("):-1]
        if not module.startswith(low):
            continue
        tail = module[len(low):]
        if tail and not _RE_CMAKE_MODULE_VER_TAIL.match(tail):
            continue
        if isinstance(srcs, (set, frozenset, list, tuple)):
            out |= {s for s in srcs if s}
    return out


def ghc_libhs_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    """libHSfoo-1.2.3-<hash>-ghc9.10.3.so()(64bit) → ghc-foo."""
    primary = re.split(r"\s*(?:<=|>=|==|!=|<|>|=)\s*", (cap or "").strip(), maxsplit=1)[0]
    m = _RE_GHC_LIBHS.match(primary)
    if not m:
        return set()
    known = package_names or set()
    name = m.group(1)
    out = _pmap_sources(provider_map, cap, primary)
    out |= _hit(known, f"ghc-{name}", f"ghc-{name.replace('_', '-')}")
    out |= _pmap_sources(provider_map, f"ghc-libhs({name})", f"ghc-libhs({name.replace('_', '-')})")
    return {s for s in out if s}


def golang_path_to_sources(cap: str, provider_map: dict) -> set[str]:
    """golang(github.com/x/y/sub/pkg) -> the source whose module root (goipath)
    is the longest prefix of the import path. Every package inside a Go module
    lives under that module's own import path, so a prefix match against the
    root %{goipath} each go2rpm spec declares is exact, not a guess."""
    m = re.match(r"^golang\(([^)]+)\)$", (cap or "").strip())
    if not m:
        return set()
    path = m.group(1)
    best_len = -1
    best: set[str] = set()
    for key, srcs in (provider_map or {}).items():
        if key == "__lower__" or not isinstance(key, str) or not key.startswith("__goprefix__"):
            continue
        prefix = key[len("__goprefix__") :]
        if path == prefix or path.startswith(prefix + "/"):
            if len(prefix) > best_len and isinstance(srcs, (set, frozenset, list, tuple)):
                best_len = len(prefix)
                best = {s for s in srcs if s}
    return best


def _glob_is_selective(pattern: str) -> bool:
    """Reject %files globs that own a whole directory.

    A spec shipping %{_bindir}/* or %{_bindir}/[a-wyz]* registers a pattern that
    matches every binary anyone asks for. Those patterns name a directory, not a
    file, so they say nothing about who provides a given path. Strip wildcards
    and character classes, then require real literal text in the basename.
    """
    base = (pattern or "").rsplit("/", 1)[-1]
    # drop character classes before counting so [a-wyz]* is empty, not "awyz"
    no_classes = re.sub(r"\[[^\]]*\]", "", base)
    literal = re.sub(r"[*?]", "", no_classes)
    return sum(1 for ch in literal if ch.isalnum()) >= 3


def _glob_provided_sources(path: str, provider_map: dict) -> set[str]:
    if not path.startswith("/"):
        return set()
    out: set[str] = set()
    for key, srcs in provider_map.items():
        if key == "__lower__" or not isinstance(key, str) or not key.startswith("__glob__"):
            continue
        pattern = key[len("__glob__") :]
        if not _glob_is_selective(pattern):
            continue
        if fnmatch.fnmatchcase(path, pattern):
            if isinstance(srcs, (set, frozenset, list, tuple)):
                out.update(s for s in srcs if s)
    return out


def _path_prefix_is_specific(path: str) -> bool:
    """True when a %files path is a real tree, not /usr/bin or /usr/share."""
    p = (path or "").rstrip("/")
    if not p.startswith("/usr"):
        return False
    return p.count("/") >= 3


def _path_in_declared_tree(missing: str, declared: str) -> bool:
    m = (missing or "").rstrip("/")
    d = (declared or "").rstrip("/")
    if not m or not d:
        return False
    if m == d:
        return True
    if m.startswith(d + "/") and _path_prefix_is_specific(d):
        return True
    if d.startswith(m + "/") and _path_prefix_is_specific(m):
        return True
    return False


def _glob_literal_prefix(pattern: str) -> str:
    return re.split(r"[*?[]", pattern or "", maxsplit=1)[0].rstrip("/")


def file_path_to_sources(path: str, provider_map: dict, repo_index: Any | None = None) -> set[str]:
    """Who ships this path, from declared %files / globs / repo file provides only."""
    missing = (path or "").strip().rstrip("/")
    if not missing.startswith("/"):
        return set()
    out: set[str] = set()
    best = -1
    best_srcs: set[str] = set()

    def _consider(declared: str, srcs: set[str]) -> None:
        nonlocal best, best_srcs
        if not srcs or not _path_in_declared_tree(missing, declared):
            return
        score = len(declared.rstrip("/"))
        if score > best:
            best = score
            best_srcs = set(srcs)
        elif score == best:
            best_srcs |= set(srcs)

    pmap = provider_map or {}
    out |= _pmap_sources(pmap, missing)
    walk = missing
    while walk.startswith("/usr"):
        out |= _pmap_sources(pmap, walk)
        if "/" not in walk[1:]:
            break
        walk = walk.rsplit("/", 1)[0]

    for key, srcs in pmap.items():
        if key == "__lower__" or not isinstance(key, str):
            continue
        if not isinstance(srcs, (set, frozenset, list, tuple)):
            continue
        owned = {s for s in srcs if s}
        if not owned:
            continue
        if key.startswith("__glob__"):
            pattern = key[len("__glob__") :]
            lit = _glob_literal_prefix(pattern)
            matched = False
            if _glob_is_selective(pattern) and fnmatch.fnmatchcase(missing, pattern):
                matched = True
            elif _path_prefix_is_specific(lit) and (
                _path_in_declared_tree(missing, lit) or fnmatch.fnmatchcase(missing, pattern)
            ):
                matched = True
            if matched:
                _consider(lit or pattern, owned)
            continue
        if key.startswith("/"):
            _consider(key, owned)

    if repo_index is not None:
        from oreon_build.core.rpm_requires import is_distgit_source_name

        providers = getattr(repo_index, "capability_providers", None) or {}
        binary_source = getattr(repo_index, "binary_source", None) or {}
        walk = missing
        while walk.startswith("/usr"):
            pkgs = providers.get(walk) or set()
            srcs = set()
            for pkg in pkgs:
                src = binary_source.get(pkg)
                if src:
                    srcs.add(src)
                elif is_distgit_source_name(pkg):
                    srcs.add(pkg)
            _consider(walk, srcs)
            out |= srcs
            if "/" not in walk[1:]:
                break
            walk = walk.rsplit("/", 1)[0]

    if best_srcs:
        return {s for s in best_srcs if s}
    return {s for s in out if s}


def crate_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    """crate(foo/default) / crate(foo) → rust-foo (+ compat rust-foo0.37)."""
    from oreon_build.services.distgit_order import _rust_crate_name_from_package

    m = _RE_CRATE.match((cap or "").strip())
    if not m:
        return set()
    crate = (m.group(1) or "").strip()
    crate = crate.split("/", 1)[0].strip()
    if not crate or "%" in crate:
        return set()
    known = package_names or set()
    crate_forms = {crate, crate.replace("_", "-"), crate.replace("-", "_")}
    out = set()
    for form in crate_forms:
        out |= _pmap_sources(
            provider_map,
            f"crate({form})",
            f"crate({form}/default)",
            cap,
        )
        out |= _hit(known, f"rust-{form}")
    want = crate.replace("-", "_").lower()
    for pkg in known:
        if not str(pkg).startswith("rust-"):
            continue
        extracted = _rust_crate_name_from_package(pkg)
        if extracted and extracted.replace("-", "_").lower() == want:
            out.add(pkg)
    return {s for s in out if s}


def path_or_binary_to_sources(
    cap: str, package_names: set[str], provider_map: dict
) -> set[str]:
    """Resolve a file path or binary name off declared file provides.

    Chopping a binary name into stems and prepending python-/lib- guesses at a
    source that never claimed the file. The distgit index already maps every
    subpackage and shipped path back to its source, so look it up instead.
    """
    primary = (cap or "").strip()
    if not primary:
        return set()
    if "(" in primary:
        stem = primary.split("(", 1)[0].strip()
        if ".so" not in stem:
            return set()
        return {s for s in _pmap_sources(provider_map, primary, stem) if s}
    known = package_names or set()
    out = _pmap_sources(provider_map, primary)
    out |= _glob_provided_sources(primary, provider_map)
    if primary.startswith("/"):
        base = primary.rsplit("/", 1)[-1]
        if primary.startswith(("/usr/bin/", "/usr/sbin/")):
            out |= _pmap_sources(
                provider_map, base, f"/usr/bin/{base}", f"/usr/sbin/{base}"
            )
        return {s for s in out if s}
    # rpm spells an extras subpackage as name+extra, the base owns the files
    plus = _RE_BIN_PLUS.match(primary)
    if plus:
        out |= _pmap_sources(provider_map, plus.group(1))
        out |= _hit(known, plus.group(1))
    out |= _pmap_sources(provider_map, f"/usr/bin/{primary}", f"/usr/sbin/{primary}")
    out |= _hit(known, primary)
    return {s for s in out if s}


_SUBPKG_SUFFIXES = (
    "-devel",
    "-static",
    "-libs",
    "-headers",
)


def subpkg_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    """A -devel/-libs subpackage belongs to whoever declares its base name.

    rpm splits one source into name and name-devel, and a spec that renamed itself
    keeps the old name as a plain Provides. wxGTK declares wxGTK3, so wxGTK3-devel
    is wxGTK. Only a declared provide counts here. Taking the bare stem as a source
    name would invent an owner for every foo-devel nobody ships.

    Suffixes are limited to the ones rpm only ever uses for a split subpackage.
    -doc and -tools show up as whole package names (yelp-tools, qt5-doc), so
    stripping them hands the cap to the wrong source.

    An ambiguous base is dropped. clang-devel strips to clang, which half a dozen
    versioned compiler specs declare, and picking among them spawns junk rebuilds.
    """
    primary = (cap or "").strip()
    if not primary or "(" in primary or primary.startswith("/"):
        return set()
    # the full name already resolves, so it names a package rather than a split
    if primary in (package_names or set()) or _pmap_sources(provider_map, primary):
        return set()
    low = primary.lower()
    out: set[str] = set()
    for suf in _SUBPKG_SUFFIXES:
        if not low.endswith(suf):
            continue
        base = primary[: -len(suf)].strip()
        if base:
            out |= _pmap_sources(provider_map, base)
    out = {s for s in out if s}
    return out if len(out) == 1 else set()


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
    out = _pmap_sources(provider_map, cap, cap.lower())
    return {s for s in out if s}


def _normalized_cap_forms(cap: str) -> list[str]:
    """Equivalent spellings of one cap, not candidate other packages.

    python3.14dist(x) and python3dist(x) are the same provide written for a
    specific interpreter, so a lookup has to try both.
    """
    c = (cap or "").strip()
    if not c:
        return []
    forms = [c]
    m = _RE_PYTHON3DIST.match(c)
    if m:
        generic = f"python3dist({m.group(1).strip()})"
        if generic != c:
            forms.append(generic)
    return forms


def _virtual_cap_leaves(requirement: str) -> list[str]:
    raw = (requirement or "").strip()
    if not raw:
        return []
    if raw.startswith("(") or " with " in raw.lower() or " or " in raw.lower():
        try:
            from oreon_build.core.rpm_requires import requirement_check_capabilities

            leaves = [c for c in requirement_check_capabilities(raw) if c]
            if leaves:
                return leaves
        except Exception:
            pass
    primary = re.split(r"\s*(?:<=|>=|==|!=|<|>|=)\s*", raw, maxsplit=1)[0].strip()
    # unwrap only a single outer paren pair, never strip cmake(...)/pkgconfig(...) closers
    if (
        primary.startswith("(")
        and primary.endswith(")")
        and primary.count("(") == 1
        and primary.count(")") == 1
    ):
        primary = primary[1:-1].strip()
        primary = re.split(
            r"\s*(?:<=|>=|==|!=|<|>|=)\s*", primary, maxsplit=1
        )[0].strip()
    return [primary] if primary else []


def distgit_sources_for_virtual_cap(
    requirement: str,
    *,
    package_names: set[str],
    provider_map: dict | None = None,
) -> set[str]:
    known = package_names or set()
    pmap = provider_map or {}
    out: set[str] = set()
    for leaf in _virtual_cap_leaves(requirement):
        for primary in _normalized_cap_forms(leaf):
            if primary in known:
                out.add(primary)
            out |= _pmap_sources(pmap, primary)
            out |= font_binary_to_sources(primary, known)
            out |= font_cap_to_sources(primary, known, pmap)
            out |= pkgconfig_to_sources(primary, known, pmap)
            out |= python3dist_to_sources(primary, known, pmap)
            out |= tex_to_sources(primary, known, pmap)
            out |= mvn_to_sources(primary, known, pmap)
            out |= ghc_devel_to_sources(primary, known, pmap)
            out |= ghc_rpm_to_sources(primary, known, pmap)
            out |= ghc_prof_to_sources(primary, known, pmap)
            out |= ghc_libhs_to_sources(primary, known, pmap)
            out |= cmake_to_sources(primary, known, pmap)
            out |= mono_to_sources(primary, known, pmap)
            out |= golang_path_to_sources(primary, pmap)
            out |= crate_to_sources(primary, known, pmap)
            out |= subpkg_to_sources(primary, known, pmap)
            out |= python_rpm_to_sources(primary, known)
            out |= path_or_binary_to_sources(primary, known, pmap)
    return {s for s in out if s}


def python_rpm_to_sources(cap: str, package_names: set[str]) -> set[str]:
    """python3-foo binary vs python-foo distgit source, same package."""
    p = (cap or "").strip()
    if not p or "(" in p or p.startswith("/"):
        return set()
    known = package_names or set()
    if p in known:
        return {p}
    if p.startswith("python3-"):
        alt = "python-" + p[8:]
        if alt in known:
            return {alt}
    elif p.startswith("python-"):
        alt = "python3-" + p[7:]
        if alt in known:
            return {alt}
    return set()


def golang_import_suggestion(cap: str) -> str | None:
    """golang(github.com/foo/bar/...) → golang-github-foo-bar."""
    m = re.match(r"^golang\(([^)]+)\)$", (cap or "").strip())
    if not m:
        return None
    path = (m.group(1) or "").strip().strip("/")
    if not path or "%" in path:
        return None
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[0] in ("github.com", "gitlab.com", "bitbucket.org"):
        host = parts[0].split(".", 1)[0]
        return f"golang-{host}-{parts[1]}-{parts[2]}"
    if len(parts) >= 2 and "." in parts[0]:
        host = parts[0].replace(".", "-")
        return f"golang-{host}-{parts[1]}"
    return None


def python3dist_import_suggestion(cap: str) -> str | None:
    m = _RE_PYTHON3DIST.match((cap or "").strip().split(" ", 1)[0])
    if not m:
        return None
    inner = (m.group(1) or "").strip()
    inner = _RE_PY_EXTRA.sub("", inner).strip()
    if not inner or "%" in inner:
        return None
    if inner.lower().startswith("python-"):
        return f"python-{inner[7:].replace('_', '-')}"
    return f"python-{inner.replace('_', '-')}"


def import_suggestion_for_cap(requirement: str) -> str | None:
    """When nothing is in distgit, prefer a package-shaped import name over a virtual cap."""
    from oreon_build.core.rpm_requires import is_spurious_solver_miss, requirement_check_capabilities

    raw = (requirement or "").strip()
    for cap in requirement_check_capabilities(raw):
        cm = _RE_CRATE.match(cap)
        if cm:
            crate = (cm.group(1) or "").strip()
            if crate and "%" not in crate:
                return f"rust-{crate}"
        py = python3dist_import_suggestion(cap)
        if py:
            return py
        go = golang_import_suggestion(cap)
        if go:
            return go
        if cap.startswith("rust-") and cap != "rust" and not is_spurious_solver_miss(cap):
            return cap
    primary = re.split(r"\s*(?:<=|>=|==|!=|<|>|=)\s*", raw, maxsplit=1)[0].strip()
    cm = _RE_CRATE.match(primary)
    if cm:
        crate = (cm.group(1) or "").strip()
        if crate and "%" not in crate:
            return f"rust-{crate}"
    py = python3dist_import_suggestion(primary)
    if py:
        return py
    go = golang_import_suggestion(primary)
    if go:
        return go
    tex = tex_import_suggestion(primary)
    if tex:
        return tex
    m = _RE_GHC_DEVEL.match(primary) or _RE_GHC_PROF.match(primary)
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
