"""Map virtual RPM caps to distgit source package names.

Discovery only. Never used as installability proof.
"""

from __future__ import annotations

import fnmatch
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
_RE_GHC_PROF = re.compile(r"^ghc-prof\((.+)\)$", re.IGNORECASE)
_RE_MONO = re.compile(r"^mono\((.+)\)$", re.IGNORECASE)
_RE_CMAKE = re.compile(r"^cmake\((.+)\)$", re.IGNORECASE)
_RE_FONT = re.compile(r"^font\((.+)\)$", re.IGNORECASE)
_RE_PC_VER = re.compile(r"^(.*?)-(\d+(?:\.\d+)*)$")
_RE_GHC_INNER = re.compile(r"^(.+)-(\d[\w.]*)-([A-Za-z0-9]+)$")
_RE_PY_EXTRA = re.compile(r"\[([^\]]*)\]\s*$")
_RE_BIN_PLUS = re.compile(r"^(.+)\+([A-Za-z0-9_.+-]+)$")
_RE_CRATE = re.compile(r"^crate\(([^)/]+)(?:/[^)]+)?\)$")

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

# common pypi name → rpm leaf (without python- prefix)
_PYTHON_DIST_ALIASES = {
    "pywavelets": ("pywt", "pywavelets"),
    "wxpython": ("wxpython4", "wxpython"),
    "python-snappy": ("snappy", "python-snappy"),
    "pyyaml": ("yaml", "pyyaml"),
    "beautifulsoup4": ("beautifulsoup4", "bs4"),
    "pillow": ("pillow", "pil"),
    "msgpack": ("msgpack", "msgpack-python"),
    "protobuf": ("protobuf", "proto-plus"),
}

_PC_NAME_SUFFIXES = (
    "Core",
    "Module",
    "Modules",
    "Utils",
    "Config",
    "Client",
    "Server",
    "Widgets",
    "Gui",
    "UI",
)
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


def _norm_alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


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


def font_cap_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    """font(wenquanyizenhei) → wqy-zenhei-fonts."""
    m = _RE_FONT.match((cap or "").strip())
    if not m:
        return set()
    inner = (m.group(1) or "").strip()
    if not inner or "%" in inner:
        return set()
    known = package_names or set()
    out = _pmap_sources(provider_map, f"font({inner})", cap)
    token = _norm_alnum(inner)
    if not token:
        return out
    for pkg in known:
        if not pkg.endswith("-fonts"):
            continue
        body = pkg[: -len("-fonts")]
        norm = _norm_alnum(body)
        if not norm:
            continue
        if norm == token or norm in token or token in norm:
            out.add(pkg)
            continue
        # wqyzenhei ⊂ wenquanyizenhei via last component zenhei
        last = body.split("-")[-1].lower()
        if len(last) >= 4 and last in token and any(
            p and p[0].lower() in token for p in body.split("-")[:-1]
        ):
            out.add(pkg)
    return {s for s in out if s}


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
        base, ver = vm.group(1), vm.group(2)
        add_from_map(f"pkgconfig({base})")
        # any pkgconfig(base-*) in map
        prefix = f"pkgconfig({base}-"
        for key, srcs in provider_map.items():
            if key == "__lower__" or not isinstance(srcs, (set, frozenset, list, tuple)):
                continue
            if key.startswith(prefix) and key.endswith(")"):
                out.update(s for s in srcs if s)
        # mozjs-140 → mozjs140 / mozjs140-devel (version baked into the name)
        major = ver.split(".", 1)[0]
        nodots = ver.replace(".", "")
        for concat in (f"{base}{ver}", f"{base}{major}", f"{base}{nodots}"):
            out |= _hit(known, concat, f"{concat}-devel")
            add_from_map(f"pkgconfig({concat})")
    # Fcitx5Core → try Fcitx5 prefix in map + fcitx5 source
    stripped = inner
    for suf in _PC_NAME_SUFFIXES:
        if stripped.endswith(suf) and len(stripped) > len(suf):
            stripped = stripped[: -len(suf)]
            add_from_map(f"pkgconfig({stripped})")
            break
    low = stripped.lower()
    out |= _hit(
        known,
        base,
        f"{base}-devel",
        base[3:] if base.startswith("lib") else "",
        f"{base[3:]}-devel" if base.startswith("lib") else "",
        low,
        f"{low}-devel",
        stripped,
        f"{stripped}-devel",
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
    # dask[array] / versioneer[toml] → base dist name
    inner = _RE_PY_EXTRA.sub("", inner).strip()
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
    # python-snappy dist name → also snappy
    if inner.lower().startswith("python-"):
        rest = inner[7:]
        variants.update(
            {
                rest,
                rest.lower(),
                rest.replace("_", "-"),
                rest.replace("-", "_"),
            }
        )
    for alias in _PYTHON_DIST_ALIASES.get(inner.lower(), ()):
        variants.add(alias)
        variants.add(alias.replace("_", "-"))
        variants.add(alias.replace("-", "_"))
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
        # bare name only when no python-* source (jupyterlab)
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


def _strip_dll_versions(stem: str) -> str:
    s = stem or ""
    # libhunspell-1.7-0 → libhunspell
    while True:
        nxt = re.sub(r"[-._]\d+$", "", s)
        if nxt == s:
            break
        s = nxt
    return s


def mingw_dll_to_sources(cap: str, package_names: set[str]) -> set[str]:
    m = _RE_MINGW_DLL.match((cap or "").strip())
    if not m:
        return set()
    known = package_names or set()
    arch = m.group(1)
    dll = m.group(2)
    stem = dll[:-4] if dll.lower().endswith(".dll") else dll
    stem = _strip_dll_versions(stem)
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
    base = inner
    vm = _RE_PC_VER.match(inner)
    if vm:
        base = vm.group(1)
        out |= _pmap_sources(provider_map, f"cmake({base})")
    out |= _hit(
        known,
        base,
        f"{base}-devel",
        f"lib{base}",
        f"lib{base}-devel",
    )
    # bson-1.0 / bson → mongo-c-driver (ships libbson)
    if base.lower() in {"bson", "mongoc", "libbson", "libmongoc"}:
        out |= _hit(known, "mongo-c-driver", "libbson", "libmongoc")
    return {s for s in out if s}


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


def _glob_provided_sources(path: str, provider_map: dict) -> set[str]:
    if not path.startswith("/"):
        return set()
    out: set[str] = set()
    for key, srcs in provider_map.items():
        if key == "__lower__" or not isinstance(key, str) or not key.startswith("__glob__"):
            continue
        pattern = key[len("__glob__") :]
        if fnmatch.fnmatchcase(path, pattern):
            if isinstance(srcs, (set, frozenset, list, tuple)):
                out.update(s for s in srcs if s)
    return out


def crate_to_sources(cap: str, package_names: set[str], provider_map: dict) -> set[str]:
    """crate(foo/default) / crate(foo) → rust-foo (+ compat rust-foo0.37)."""
    from oreon_build.services.distgit_order import _rust_crate_name_from_package

    m = _RE_CRATE.match((cap or "").strip())
    if not m:
        return set()
    crate = (m.group(1) or "").strip()
    if not crate or "%" in crate:
        return set()
    known = package_names or set()
    out = _pmap_sources(
        provider_map,
        f"crate({crate})",
        f"crate({crate}/default)",
        cap,
    )
    out |= _hit(known, f"rust-{crate}")
    for pkg in known:
        if not pkg.startswith("rust-"):
            continue
        if _rust_crate_name_from_package(pkg) == crate:
            out.add(pkg)
    return {s for s in out if s}


_RUNTIME_PACKAGE_STEMS = frozenset(
    {
        "rust",
        "go",
        "golang",
        "python",
        "python2",
        "python3",
        "perl",
        "ruby",
        "node",
        "nodejs",
        "php",
        "lua",
        "ghc",
        "java",
        "javac",
        "ocaml",
        "R",
        "r",
    }
)


def path_or_binary_to_sources(
    cap: str, package_names: set[str], provider_map: dict
) -> set[str]:
    """/usr/bin/sphinx-build / sphinx-build → python-sphinx."""
    from oreon_build.services.distgit_order import _rust_crate_name_from_package

    primary = (cap or "").strip()
    if not primary or "(" in primary:
        return set()
    known = package_names or set()
    out = _pmap_sources(provider_map, primary)
    out |= _glob_provided_sources(primary, provider_map)
    base = primary
    if primary.startswith("/usr/bin/") or primary.startswith("/usr/sbin/"):
        base = primary.rsplit("/", 1)[-1]
        out |= _pmap_sources(provider_map, base, f"/usr/bin/{base}", f"/usr/sbin/{base}")
    elif "/" in primary:
        return {s for s in out if s}
    if base.startswith("rust-") and base != "rust":
        out |= _hit(known, base)
        crate = _rust_crate_name_from_package(base)
        if crate:
            out |= crate_to_sources(f"crate({crate})", known, provider_map)
        return {s for s in out if s}
    if base.startswith(("python3-", "python-")) and base not in _RUNTIME_PACKAGE_STEMS:
        out |= _hit(known, base)
        if base.startswith("python3-"):
            rest = base[len("python3-") :]
            out |= _hit(known, f"python-{rest}", f"python3-{rest}")
            if rest:
                out |= python3dist_to_sources(f"python3dist({rest})", known, provider_map)
                out |= python3dist_to_sources(
                    f"python3dist({rest.replace('-', '_')})", known, provider_map
                )
        else:
            rest = base[len("python-") :]
            out |= _hit(known, f"python-{rest}", f"python3-{rest}")
            if rest:
                out |= python3dist_to_sources(f"python3dist({rest})", known, provider_map)
                out |= python3dist_to_sources(
                    f"python3dist({rest.replace('-', '_')})", known, provider_map
                )
        return {s for s in out if s}
    if base.startswith(("golang-", "perl-", "rubygem-", "nodejs-", "ghc-", "php-")):
        out |= _hit(known, base)
        return {s for s in out if s}
    # python3-pytest-xdist+psutil
    plus = _RE_BIN_PLUS.match(base)
    if plus:
        base = plus.group(1)
        out |= _pmap_sources(provider_map, base)
    out |= devel_subpkg_to_sources(base, known)
    out |= _hit(known, base)
    # sphinx-build → python-sphinx (not the unrelated sphinx search package)
    parts = [p for p in base.split("-") if p]
    for i in range(len(parts), 0, -1):
        stem = "-".join(parts[:i])
        if stem in _RUNTIME_PACKAGE_STEMS or stem.lower() in _RUNTIME_PACKAGE_STEMS:
            continue
        py_hits = _hit(known, f"python-{stem}", f"python3-{stem}")
        out |= py_hits
        out |= _pmap_sources(
            provider_map,
            f"python3-{stem}",
            f"python-{stem}",
            f"/usr/bin/{stem}",
            stem,
        )
        if py_hits:
            break
        bare = _hit(known, f"{stem}-cli")
        # bare stem only when no python-* exists for this token
        if not _hit(known, f"python-{stem}", f"python3-{stem}"):
            bare |= _hit(known, stem)
        out |= bare
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
    """Map binary subpackages like llvm15-static back to distgit source llvm15."""
    n = (cap or "").strip()
    known = package_names or set()
    if not n or n in known:
        return _hit(known, n) if n else set()
    # python3-foo+extra → python-foo
    plus = _RE_BIN_PLUS.match(n)
    if plus:
        n = plus.group(1)
        if n in known:
            return {n}
    # longer suffixes first so cmake-utils beats utils
    for suffix in (
        "-cmake-utils",
        "-debugsource",
        "-debuginfo",
        "-devel",
        "-static",
        "-libs",
        "-docs",
        "-doc",
        "-tests",
        "-test",
        "-common",
        "-tools",
        "-utils",
        "-prof",
    ):
        if n.endswith(suffix):
            base = n[: -len(suffix)]
            hit = _hit(known, base)
            if hit:
                return hit
    if n.startswith("python3-"):
        rest = n[len("python3-") :]
        # dont map python3-subunit → subunit (C bindings). only python-* sources.
        hit = _hit(known, f"python-{rest}", f"python3-{rest}")
        if hit:
            return hit
    return set()


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
    for primary in _virtual_cap_leaves(requirement):
        if primary in known:
            out.add(primary)
        out |= font_binary_to_sources(primary, known)
        out |= font_cap_to_sources(primary, known, pmap)
        out |= pkgconfig_to_sources(primary, known, pmap)
        out |= python3dist_to_sources(primary, known, pmap)
        out |= tex_to_sources(primary, known, pmap)
        out |= mvn_to_sources(primary, known, pmap)
        out |= mingw_dll_to_sources(primary, known)
        out |= ghc_devel_to_sources(primary, known, pmap)
        out |= ghc_prof_to_sources(primary, known, pmap)
        out |= ghc_libhs_to_sources(primary, known, pmap)
        out |= cmake_to_sources(primary, known, pmap)
        out |= mono_to_sources(primary, known, pmap)
        out |= golang_path_to_sources(primary, pmap)
        out |= crate_to_sources(primary, known, pmap)
        out |= devel_subpkg_to_sources(primary, known)
        out |= path_or_binary_to_sources(primary, known, pmap)
    return {s for s in out if s}


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
