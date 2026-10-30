# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Write an Oreon spec from the upstream tarball, not from another distro spec."""

from __future__ import annotations

import re
from pathlib import Path

import click

LOOKASIDE = ("src.fedoraproject.org", "download.fedoraproject.org", "lookaside")
LICENSE_NAMES = (
    "COPYING",
    "COPYING.LIB",
    "COPYING.LESSER",
    "COPYING-LGPLV3",
    "LICENSE",
    "LICENSE.txt",
    "LICENSE.md",
    "LICENCE",
    "COPYRIGHT",
)
TOOL_BR = {
    "m4": "m4",
    "flex": "flex",
    "bison": "bison",
    "python3": "python3",
    "python": "python3",
    "pkg-config": "pkgconf",
    "pkgconf": "pkgconf",
    "gettext": "gettext",
    "gperf": "gperf",
    "ragel": "ragel",
    "wayland-scanner": "wayland-scanner",
}
# programs that are already in the buildroot, or not packages at all
BASE_TOOLS = {
    "sh", "bash", "sed", "awk", "gawk", "grep", "egrep", "cat", "cp", "mv", "rm",
    "ln", "mkdir", "chmod", "install", "true", "false", "echo", "env", "make",
    "diff", "cmp", "sort", "uniq", "head", "tail", "tr", "find", "xargs", "touch",
    "which", "uname", "rmdir", "basename", "dirname", "cut", "tee", "printf",
    "test", "ls", "pwd", "sleep", "date", "cc", "gcc", "g++", "ar", "ld", "strip",
    "pkg-config", "pkgconf", "cmake", "meson", "ninja", "git",
}
# meson dependency() names that are compiler or libc features, not pkg-config modules
BUILTIN_DEPS = {
    "atomic", "blocks", "boost", "c", "coarray", "dl", "fortran", "iconv", "intl",
    "libatomic", "m", "mpi", "objc", "objcpp", "openmp", "pthread", "python",
    "python3", "rt", "threads",
}
SKIP_DIRS = {
    "subprojects", "third_party", "thirdparty", "vendor", "external", "extern",
    ".git", "tests", "test", "testing", "examples", "benchmarks", "fuzz", "doc", "docs",
}


def default_spec_root() -> Path:
    here = Path(__file__).resolve().parent.parent
    for cand in (
        here / "rpm-specfiles" / "oreon-11-rp1",
        here / "fedora-rpms" / "oreon-11-rp1",
        Path.cwd() / "oreon-11-rp1",
    ):
        if cand.is_dir():
            return cand
    return here / "rpm-specfiles" / "oreon-11-rp1"


def refuse_lookaside(url: str) -> None:
    low = (url or "").lower()
    for bit in LOOKASIDE:
        if bit in low:
            raise click.ClickException("refusing lookaside url %s" % url)


def templated_source(url: str, version: str) -> str:
    if version and url.count(version) == 1:
        return url.replace(version, "%{version}")
    return url


def version_from_filename(url: str) -> str | None:
    name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    ext = r"(?:tar\.(?:gz|bz2|xz|zst)|tgz|zip|crate)"
    for pattern in (
        rf"^v?(\d+(?:\.\d+)+(?:[-.][0-9A-Za-z]+)*)\.{ext}$",
        rf"[-_]v?(\d+(?:\.\d+)+(?:[-.][0-9A-Za-z]+)*)\.{ext}$",
        rf"^v?(\d{{8}})\.{ext}$",
        rf"[-_](\d{{8}})\.{ext}$",
    ):
        m = re.search(pattern, name)
        if m:
            return m.group(1)
    return None


def strip_hash_comments(text: str) -> str:
    out = []
    i = 0
    quote = ""
    while i < len(text):
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "#":
            nl = text.find("\n", i)
            if nl < 0:
                break
            out.append("\n")
            i = nl + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def iter_calls(text: str, name: str):
    token = name + "("
    i = 0
    while True:
        j = text.find(token, i)
        if j < 0:
            return
        if j > 0 and (text[j - 1].isalnum() or text[j - 1] == "_"):
            i = j + len(name)
            continue
        k = j + len(token)
        depth = 1
        start = k
        while k < len(text) and depth:
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
            k += 1
        yield text[start : k - 1]
        i = k


def kw_string(body: str, key: str) -> str | None:
    m = re.search(r"%s\s*:\s*'([^']*)'" % re.escape(key), body)
    if m:
        return m.group(1)
    m = re.search(r'%s\s*:\s*"([^"]*)"' % re.escape(key), body)
    return m.group(1) if m else None


def m4_args(body: str) -> list[str]:
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    for c in body:
        if c == "[":
            depth += 1
            buf.append(c)
        elif c == "]":
            depth = max(0, depth - 1)
            buf.append(c)
        elif c == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
    if "".join(buf).strip():
        args.append("".join(buf).strip())
    return args


def unbracket(s: str) -> str:
    s = s.strip()
    while s.startswith("[") and s.endswith("]") and s.count("[") == s.count("]"):
        s = s[1:-1].strip()
    return s


def pc_module(token: str) -> str | None:
    token = token.strip().strip("[]'\"")
    token = re.split(r"(?:>=|<=|!=|=|>|<)", token, maxsplit=1)[0].strip()
    if not token or token[0] in "$%@/-.":
        return None
    low = token.lower()
    if low in BUILTIN_DEPS or low in ("required", "quiet", "imported_target"):
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", token):
        return None
    if token.endswith((".pc", ".h", ".so", ".a", ".c")):
        return None
    return token


def pkg_modules(blob: str) -> list[str]:
    mods = []
    for part in blob.replace(",", " ").split():
        name = pc_module(part)
        if name and name not in mods:
            mods.append(name)
    return mods


def tool_package(name: str) -> str | None:
    name = name.strip().strip("'\"")
    if not name or "/" in name or name.startswith("."):
        return None
    if name.lower() in BASE_TOOLS:
        return None
    mapped = TOOL_BR.get(name, TOOL_BR.get(name.lower()))
    if mapped:
        return mapped
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", name):
        return name
    return None


def short_text(text: str, limit: int = 90) -> str:
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or "")
    line = re.sub(r"[*_`]", "", line)
    line = re.sub(r"\s+", " ", line.strip())
    line = line.strip(" .")
    if len(line) > limit:
        line = line[: limit - 3].rsplit(" ", 1)[0]
    return line


def license_files(root: Path) -> list[str]:
    found = []
    for name in LICENSE_NAMES:
        if (root / name).is_file():
            found.append(name)
    if found:
        return found
    extra = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        low = path.name.lower()
        if low.startswith(("copying", "license", "licence")) and path.stat().st_size < 200000:
            extra.append(path.name)
    return extra[:4]


def normalize_license(hinted: str | None) -> str | None:
    if not hinted:
        return None
    text = hinted.strip()
    if re.fullmatch(r"MIT( license)?", text, re.I):
        return "MIT"
    return text


def sniff_license(root: Path, hinted: str | None) -> str:
    hinted = normalize_license(hinted)
    if hinted:
        return hinted
    for name in license_files(root):
        try:
            blob = (root / name).read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        up = blob.upper()
        if "GNU LESSER GENERAL PUBLIC LICENSE" in up or "GNU LIBRARY GENERAL PUBLIC LICENSE" in up:
            if "VERSION 3" in up:
                return "LGPL-3.0-or-later"
            return "LGPL-2.1-or-later"
        if "GNU GENERAL PUBLIC LICENSE" in up:
            if "VERSION 3" in up:
                return "GPL-3.0-or-later"
            if "VERSION 2" in up:
                return "GPL-2.0-or-later"
        if "APACHE LICENSE" in up and "VERSION 2" in up:
            return "Apache-2.0"
        if "PERMISSION IS HEREBY GRANTED" in up:
            return "MIT"
        if "BSD" in up and "REDISTRIBUTION AND USE" in up:
            return "BSD-3-Clause"
    return "LicenseRef-Unknown"


def meson_tree_files(root: Path) -> list[Path]:
    seen: list[Path] = []

    def walk(path: Path) -> None:
        if path in seen or not path.is_file() or skipped_rel(root, path):
            return
        seen.append(path)
        text = strip_hash_comments(path.read_text(encoding="utf-8", errors="replace"))
        for m in re.finditer(r"subdir\(\s*'([^']+)'", text):
            entry = m.group(1)
            if any(part.lower() in SKIP_DIRS for part in Path(entry).parts):
                continue
            walk(path.parent / entry / "meson.build")

    walk(root / "meson.build")
    return seen


def meson_options(root: Path) -> dict[str, str]:
    path = root / "meson_options.txt"
    if not path.is_file():
        return {}
    text = strip_hash_comments(path.read_text(encoding="utf-8", errors="replace"))
    out = {}
    for body in iter_calls(text, "option"):
        m = re.match(r"\s*'([^']+)'", body)
        if not m:
            continue
        out[m.group(1)] = kw_string(body, "value") or ""
    return out


def required_get_option(expr: str | None, options: dict[str, str]) -> bool:
    if expr is None:
        return True
    low = expr.strip().lower()
    if low in ("false", "no"):
        return False
    if low in ("true", "yes"):
        return True
    m = re.search(r"get_option\(\s*'([^']+)'", expr)
    if not m:
        return True
    value = (options.get(m.group(1)) or "auto").lower()
    return value not in ("disabled", "false", "no")


def inspect_meson(root: Path) -> dict:
    files = meson_tree_files(root)
    if not files:
        return {}
    options = meson_options(root)
    text = "\n".join(strip_hash_comments(p.read_text(encoding="utf-8", errors="replace")) for p in files)
    project = next(iter_calls(text, "project"), "")
    langs = []
    m = re.match(r"\s*'[^']+'\s*,\s*(\[[^\]]*\]|'[^']+')", project)
    if m:
        langs = re.findall(r"'([^']+)'", m.group(1))
    deps = []
    for body in iter_calls(text, "dependency"):
        name = re.match(r"\s*'([^']+)'", body)
        if not name:
            continue
        if not required_get_option(kw_string(body, "required") or _required_expr(body), options):
            continue
        mod = pc_module(name.group(1))
        if mod and mod not in deps:
            deps.append(mod)
    tools = []
    for body in iter_calls(text, "find_program"):
        name = re.match(r"\s*'([^']+)'", body)
        if not name:
            continue
        if not required_get_option(kw_string(body, "required") or _required_expr(body), options):
            continue
        tool = tool_package(name.group(1))
        if tool and tool not in tools:
            tools.append(tool)
    libs = meson_installed_libs(text)
    shared = bool(libs)
    headers = "install_headers" in text or "pkg.generate" in text or ".generate(" in text or shared
    summary = kw_string(project, "description") or ""
    version = kw_string(project, "version") or ""
    license_s = kw_string(project, "license")
    if license_s is None:
        listed = re.search(r"license\s*:\s*\[([^\]]*)\]", project)
        if listed:
            bits = re.findall(r"'([^']+)'", listed.group(1))
            license_s = " OR ".join(bits) if bits else None
    return {
        "system": "meson",
        "languages": langs or ["c"],
        "deps": deps,
        "tools": tools,
        "libs": libs,
        "shared": shared,
        "headers": headers or shared,
        "summary": summary,
        "version": version,
        "license": license_s,
        "url": kw_string(project, "url") or "",
        "flags": [],
    }


def meson_installed_libs(text: str) -> list[str]:
    found = []
    for kind in ("shared_library", "both_libraries", "library"):
        for body in iter_calls(text, kind):
            if not re.search(r"install\s*:\s*true", body):
                continue
            name = re.match(r"\s*'([^']+)'", body)
            if name and name.group(1) not in found:
                found.append(name.group(1))
    return found


def _required_expr(body: str) -> str | None:
    m = re.search(r"required\s*:\s*(get_option\([^)]*\)|true|false)", body)
    return m.group(1) if m else None


def inspect_autotools(root: Path) -> dict:
    path = root / "configure.ac"
    if not path.is_file():
        path = root / "configure.in"
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("dnl "))
    init = next(iter_calls(text, "AC_INIT"), "")
    args = m4_args(init)
    version = unbracket(args[1]) if len(args) > 1 else ""
    urls = []
    for arg in args[2:]:
        val = unbracket(arg)
        if val.startswith("http"):
            urls.append(val)
    url = urls[-1] if urls else ""
    deps = []
    for m in re.finditer(r"PKG_CHECK_MODULES\s*\((.*?)\)", text, re.S):
        bits = m4_args(m.group(1))
        if len(bits) < 2:
            continue
        fail = unbracket(bits[3]) if len(bits) > 3 else ""
        if len(bits) > 3 and fail and "AC_MSG_ERROR" not in fail and "AC_MSG_FAILURE" not in fail:
            continue
        deps.extend(x for x in pkg_modules(unbracket(bits[1])) if x not in deps)
    tools = []
    if re.search(r"\bm4\b", text) and re.search(r"AC_MSG_ERROR\([^\)]*m4", text):
        tools.append("m4")
    makefiles = automake_files(root)
    blob = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in makefiles)
    libs = automake_libs(blob)
    shared = bool(libs) or bool(re.search(r"\bLT_INIT\b|\bAC_PROG_LIBTOOL\b", text))
    headers = "_HEADERS" in blob or "install_headers" in text or bool(libs)
    langs = ["c"]
    if re.search(r"\bAC_PROG_CXX\b", text):
        langs.append("cpp")
    flags = []
    if re.search(r"enable_static|disable-static|LT_INIT|AC_PROG_LIBTOOL", text):
        flags.append("--disable-static")
    return {
        "system": "autotools",
        "languages": langs,
        "deps": deps,
        "tools": tools,
        "libs": libs,
        "shared": shared,
        "headers": headers or shared,
        "summary": "",
        "version": version if version and "%" not in version and not version.startswith("$") else "",
        "license": None,
        "url": url,
        "flags": flags,
    }


def skipped_rel(root: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        return True
    return any(part.lower() in SKIP_DIRS for part in parts)


def automake_files(root: Path) -> list[Path]:
    seen: list[Path] = []

    def walk(path: Path) -> None:
        if path in seen or not path.is_file() or skipped_rel(root, path):
            return
        seen.append(path)
        text = re.sub(r"\\\n", " ", path.read_text(encoding="utf-8", errors="replace"))
        for match in re.finditer(r"(?m)^SUBDIRS\s*=\s*(.+)$", text):
            for entry in match.group(1).split():
                if entry.startswith(("$", "@", ".")):
                    continue
                walk(path.parent / entry / "Makefile.am")

    walk(root / "Makefile.am")
    return seen


def automake_libs(text: str) -> list[str]:
    text = re.sub(r"\\\n", " ", text)
    found = []
    for match in re.finditer(r"(?m)^lib_LTLIBRARIES\s*=\s*(.+)$", text):
        for item in match.group(1).split():
            if item.endswith(".la"):
                item = item[:-3]
            if item and item not in found:
                found.append(item)
    return found


def cmake_files(root: Path) -> list[Path]:
    seen: list[Path] = []

    def walk(path: Path) -> None:
        if path in seen or not path.is_file() or skipped_rel(root, path):
            return
        seen.append(path)
        text = strip_hash_comments(path.read_text(encoding="utf-8", errors="replace"))
        for match in re.finditer(r'add_subdirectory\s*\(\s*"?([A-Za-z0-9_./+-]+)', text):
            entry = match.group(1)
            if any(part.lower() in SKIP_DIRS for part in Path(entry).parts):
                continue
            walk(path.parent / entry / "CMakeLists.txt")

    walk(root / "CMakeLists.txt")
    return seen


def cmake_installed_libs(text: str) -> list[str]:
    reserved = {
        "TARGETS", "RUNTIME", "LIBRARY", "ARCHIVE", "PUBLIC_HEADER", "PRIVATE_HEADER",
        "DESTINATION", "EXPORT", "INCLUDES", "COMPONENT", "OPTIONAL", "NAMELINK_SKIP",
        "NAMELINK_ONLY", "PERMISSIONS", "CONFIGURATIONS", "FRAMEWORK", "BUNDLE",
    }
    libs = []
    kinds = {}
    for body in iter_calls(text, "add_library"):
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)(?:\s+(SHARED|STATIC|OBJECT|INTERFACE|MODULE|ALIAS))?", body)
        if not match:
            continue
        kinds[match.group(1)] = match.group(2) or ""
    installed = []
    saw_install = False
    for body in iter_calls(text, "install"):
        if not re.match(r"\s*TARGETS\b", body, re.I):
            continue
        saw_install = True
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body):
            if token.upper() not in reserved:
                installed.append(token)
    names = installed if saw_install else list(kinds)
    for name in names:
        kind = kinds.get(name)
        if kind is None or kind in ("STATIC", "OBJECT", "INTERFACE", "MODULE", "ALIAS"):
            continue
        if kind == "" and not re.search(r"BUILD_SHARED_LIBS\s+ON", text):
            continue
        if name not in libs:
            libs.append(name)
    return libs


def inspect_cmake(root: Path) -> dict:
    files = cmake_files(root)
    if not files:
        return {}
    text = "\n".join(strip_hash_comments(p.read_text(encoding="utf-8", errors="replace")) for p in files)
    project = next(iter_calls(text, "project"), "")
    version = kw_string(project, "VERSION") or ""
    if not version:
        m = re.search(r"(?i)\bVERSION\s+(\d[\w.]*)", project)
        version = m.group(1) if m else ""
    desc = kw_string(project, "DESCRIPTION") or ""
    if not desc:
        m = re.search(r'(?i)\bDESCRIPTION\s+"([^"]*)"', project)
        desc = m.group(1) if m else ""
    home = kw_string(project, "HOMEPAGE_URL") or ""
    if not home:
        m = re.search(r'(?i)\bHOMEPAGE_URL\s+"([^"]*)"', project)
        home = m.group(1) if m else ""
    license_s = None
    m = re.search(r'(?i)PROJECT_LICENSE\s+"([^"]*)"', text)
    if m:
        license_s = m.group(1)
    langs = [x.lower() for x in re.findall(r"(?i)\bLANGUAGES\s+([A-Z0-9 ]+)", project)]
    if langs:
        langs = [part.lower() for part in langs[0].split()]
    else:
        langs = [x.lower() for x in re.findall(r"\b(C|CXX)\b", project)]
    deps = []
    for m in re.finditer(r"(?<![\w_])pkg_check_modules\s*\(([^)]*)\)", text, re.I):
        body = m.group(1)
        parts = body.replace(",", " ").split()
        upper = [part.upper() for part in parts]
        if "REQUIRED" not in upper:
            continue
        deps.extend(x for x in pkg_modules(" ".join(parts[upper.index("REQUIRED") + 1 :])) if x not in deps)
    tools = []
    if re.search(r"linux/version\.h", text) and "FATAL_ERROR" in text:
        tools.append("kernel-headers")
    if re.search(r"find_package\s*\(\s*PkgConfig\b", text):
        tools.append("pkgconf")
    libs = cmake_installed_libs(text)
    shared = bool(libs)
    flags = ["-DCMAKE_SKIP_RPATH=ON"]
    if re.search(r"BUILD_STATIC_LIBS|BUILD_SHARED_LIBS", text):
        flags.append("-DBUILD_STATIC_LIBS=OFF")
    if re.search(r"BUILD_TESTING", text):
        flags.append("-DBUILD_TESTING=OFF")
    return {
        "system": "cmake",
        "languages": langs or ["c"],
        "deps": deps,
        "tools": tools,
        "libs": libs,
        "shared": shared,
        "headers": shared,
        "summary": desc,
        "version": version,
        "license": license_s,
        "url": home,
        "flags": flags,
    }


def inspect_make(root: Path) -> dict:
    if not (root / "Makefile").is_file() and not (root / "makefile").is_file():
        return {}
    return {
        "system": "make",
        "languages": ["c"],
        "deps": [],
        "tools": [],
        "shared": False,
        "headers": False,
        "summary": "",
        "version": "",
        "license": None,
        "url": "",
        "flags": [],
    }


def inspect_tree(root: Path) -> dict:
    for fn in (inspect_meson, inspect_cmake, inspect_autotools, inspect_make):
        got = fn(root)
        if got:
            got["license_files"] = license_files(root)
            got["license"] = sniff_license(root, got.get("license"))
            if not got.get("summary"):
                got["summary"] = readme_summary(root)
            return got
    raise click.ClickException("no meson, cmake, configure.ac, or Makefile in %s" % root)


def readme_summary(root: Path) -> str:
    for name in ("README.md", "README", "README.txt"):
        path = root / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip().lstrip("#").strip()
            if len(line) < 12 or line.startswith(("=", "-", "|", "[")):
                continue
            return short_text(line)
    return ""


def topdir_macro(root: Path, name: str, version: str) -> str | None:
    base = root.name
    if base in (name, "%s-%s" % (name, version), "src"):
        if base == "%s-%s" % (name, version):
            return None
        if base == name:
            return name
    if version and version in base:
        return base.replace(version, "%{version}")
    return base


def so_globs(target: str) -> list[str]:
    if target.startswith("lib"):
        return ["/" + target + ".so."]
    return ["/lib" + target + ".so.", "/" + target + ".so."]


def lib_package_name(target: str) -> str:
    return target if target.startswith("lib") else "lib" + target


def extra_lib_packages(pkg: str, libs: list[str]) -> list[dict]:
    public = []
    seen = set()
    for target in libs:
        if "plugin" in target.lower():
            continue
        rpm = lib_package_name(target)
        if rpm in seen:
            continue
        seen.add(rpm)
        public.append((target, rpm))
    if len(public) < 2:
        return []
    primary = public[0][1]
    for _target, rpm in public:
        bare = rpm[3:] if rpm.startswith("lib") else rpm
        if pkg in (rpm, bare):
            primary = rpm
            break
    extra = []
    for target, rpm in public:
        if rpm == primary:
            continue
        extra.append({
            "n": rpm,
            "summary": rpm,
            "globs": so_globs(target),
        })
    return extra


def render_spec(name: str, version: str, source_url: str, source_hash: str, info: dict, top: str | None) -> str:
    summary = short_text(info.get("summary") or name)
    license_id = info.get("license") or "LicenseRef-Unknown"
    url = info.get("url") or homepage_from_source(source_url)
    langs = set(info.get("languages") or [])
    brs = ["gcc"] if "c" in langs or "cpp" in langs or not langs else []
    if "cpp" in langs or "c++" in langs or "cxx" in langs:
        brs.append("gcc-c++")
    if brs:
        brs.append("glibc-devel")
    system = info["system"]
    if system in ("autotools", "make"):
        brs.append("make")
    if system == "meson":
        brs.extend(["meson", "ninja-build"])
    if system == "cmake":
        brs.extend(["cmake", "ninja-build"])
    subs = extra_lib_packages(name, info.get("libs") or [])
    shared = bool(info.get("shared")) or bool(info.get("libs"))
    headers = bool(info.get("headers"))
    devel = shared or headers
    if shared:
        brs.append("patchelf")
    lines = [
        "%%global source0_hash %s" % source_hash,
        "%global oreon_bootstrap %{nil}",
        "%bcond bootstrap 0",
        "%oreon_bootstrap",
        "%global debug_package %{nil}",
        "",
        "Name: %s" % name,
        "Version: %s" % version,
        "Release: 1%{?dist}",
        "Summary: %s" % summary,
        "License: %s" % license_id,
        "URL: %s" % url,
        "Source0: %s" % templated_source(source_url, version),
        "",
        "BuildRequires: oreon-rpm-macros",
        "%if %{without bootstrap} && !0%{?bootstrap}",
    ]
    for br in brs:
        lines.append("BuildRequires: %s" % br)
    lines.append("# !!!ADD DEPS!!! !!!ADD DEPS!!! !!!ADD DEPS!!!")
    lines.append("%endif")
    lines.append("")
    lines.append("%description")
    lines.append(summary)
    for sub in subs:
        lines.extend([
            "",
            "%%package -n %s" % sub["n"],
            "Summary: %s" % sub["summary"],
            "",
            "%%description -n %s" % sub["n"],
            sub["summary"],
        ])
    if devel and shared:
        lines.extend([
            "",
            "%package devel",
            "Summary: %s" % summary,
            "Requires: %s%%{?_isa} = %%{version}-%%{release}" % name,
        ])
        for sub in subs:
            lines.append("Requires: %s%%{?_isa} = %%{version}-%%{release}" % sub["n"])
        lines.extend([
            "",
            "%description devel",
            summary,
        ])
    lines.extend(["", "%prep", "%oreon_sha256"])
    if top:
        lines.append("%%autosetup -n %s -p1" % top)
    else:
        lines.append("%autosetup -p1")
    lines.extend(["", "%build"])
    flags = info.get("flags") or []
    if system == "meson":
        if flags:
            lines.append("%oreon_meson \\")
            for i, flag in enumerate(flags):
                lines.append("  %s%s" % (flag, " \\" if i + 1 < len(flags) else ""))
        else:
            lines.append("%oreon_meson")
        lines.append("%oreon_meson_build")
        lines.extend(["", "%install", "%oreon_meson_install"])
    elif system == "cmake":
        if flags:
            lines.append("%oreon_cmake \\")
            for i, flag in enumerate(flags):
                lines.append("  %s%s" % (flag, " \\" if i + 1 < len(flags) else ""))
        else:
            lines.append("%oreon_cmake")
        lines.append("%oreon_cmake_build")
        lines.extend(["", "%install", "%oreon_cmake_install"])
    else:
        if flags:
            lines.append("%oreon_configure \\")
            for i, flag in enumerate(flags):
                lines.append("  %s%s" % (flag, " \\" if i + 1 < len(flags) else ""))
        else:
            lines.append("%oreon_configure")
        lines.append("%oreon_make")
        lines.extend(["", "%install", "%oreon_make_install"])
    if shared:
        lines.extend([
            "rm -f %{buildroot}%{_libdir}/*.la",
            "find %{buildroot} -type f | while read -r f; do",
            "  file -b \"$f\" | grep -q '^ELF' || continue",
            "  r=$(patchelf --print-rpath \"$f\")",
            "  if [ -n \"$r\" ]; then",
            "    chmod u+w \"$f\"",
            "    patchelf --remove-rpath \"$f\"",
            "  fi",
            "done",
        ])
    if devel and shared:
        lines.append("%oreon_filelist_devel")
    else:
        lines.append("%oreon_filelist")
    if subs:
        lines.extend(split_subpackages_lines(subs))
    lic = info.get("license_files") or []
    lines.append("")
    lines.append("%files -f %{_builddir}/%{name}.files")
    if lic:
        lines.append("%license " + " ".join(lic))
    if devel and shared:
        lines.append("")
        lines.append("%files devel -f %{_builddir}/%{name}-devel.files")
    for sub in subs:
        lines.append("")
        lines.append("%%files -n %s -f %%{_builddir}/%s.files" % (sub["n"], sub["n"]))
    lines.append("")
    return "\n".join(lines)


def split_subpackages_lines(subs: list[dict]) -> list[str]:
    pairs = []
    for sub in subs:
        globs = ", ".join('"%s"' % glob for glob in sub["globs"])
        pairs.append('    "%s": [%s],' % (sub["n"], globs))
    body = "\n".join(pairs)
    return [
        "python3 - %{_builddir}/%{name}.files << 'PY'",
        "import sys",
        "from pathlib import Path",
        "subs = {",
        body,
        "}",
        "main = Path(sys.argv[1])",
        "rows = main.read_text().splitlines()",
        "keep = []",
        "moved = {key: [] for key in subs}",
        "for line in rows:",
        "    item = line.split()[-1] if line else ''",
        "    hit = ''",
        "    for key, globs in subs.items():",
        "        if any(glob in item for glob in globs):",
        "            hit = key",
        "            break",
        "    if hit:",
        "        moved[hit].append(line)",
        "    else:",
        "        keep.append(line)",
        "main.write_text('\\n'.join(keep) + ('\\n' if keep else ''))",
        "for key, lines in moved.items():",
        "    Path(str(main.parent / (key + '.files'))).write_text('\\n'.join(lines) + ('\\n' if lines else ''))",
        "PY",
    ]


def homepage_from_source(url: str) -> str:
    m = re.search(r"https?://github\.com/[^/]+/[^/]+", url or "")
    if m:
        return m.group(0).split("/archive/")[0].split("/releases/")[0]
    m = re.search(r"https?://[^/]+", url or "")
    return m.group(0) if m else "https://example.invalid"


def fetch_helpers():
    try:
        from .orbscli import _fetch_source_archive, _unpack_source_archive
    except ImportError:
        from orbscli import _fetch_source_archive, _unpack_source_archive
    return _fetch_source_archive, _unpack_source_archive


def github_tarball(repo_url: str, version: str | None) -> tuple[str, str]:
    import httpx

    m = re.search(r"github\.com/([^/]+)/([^/#?]+)", repo_url)
    if not m:
        raise click.ClickException("not a github repo url")
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
    if version:
        tags = [version, "v" + version]
    else:
        with httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = client.get("https://github.com/%s/%s/releases/latest" % (owner, repo))
            response.raise_for_status()
        tag_m = re.search(r"/releases/tag/([^/?#]+)$", str(response.url))
        if not tag_m:
            raise click.ClickException("no github release tag for %s/%s" % (owner, repo))
        tag = tag_m.group(1)
        ver = tag[1:] if tag.startswith("v") and tag[1:2].isdigit() else tag
        tags = [tag]
        version = ver
    base = "https://github.com/%s/%s/archive/refs/tags/" % (owner, repo)
    with httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for tag in tags:
            url = base + tag + ".tar.gz"
            head = client.head(url)
            if head.status_code < 400:
                ver = tag[1:] if tag.startswith("v") and tag[1:2].isdigit() else tag
                return url, ver
    raise click.ClickException("github tag tarball was not there")


def _manifest_name(path: Path, pattern: str) -> str:
    if not path.is_file():
        return ""
    match = re.search(pattern, path.read_text(encoding="utf-8", errors="replace"))
    return match.group(1).strip() if match else ""


def lang_kind_from_tree(root: Path, name: str, url: str) -> tuple[str, str] | None:
    """Language ecosystems that have a *2rpm tool. C builds are not those."""
    url_l = (url or "").lower()
    c_build = any(
        (root / entry).exists()
        for entry in ("meson.build", "CMakeLists.txt", "configure.ac", "configure.in", "Makefile", "makefile")
    )
    cargo = _manifest_name(root / "Cargo.toml", r'(?m)^name\s*=\s*"([^"]+)"')
    gomod = _manifest_name(root / "go.mod", r"(?m)^module\s+(\S+)")
    pypi = _manifest_name(root / "pyproject.toml", r'(?m)^name\s*=\s*"([^"]+)"')
    gem = ""
    for spec in sorted(root.glob("*.gemspec")):
        gem = _manifest_name(spec, r"""\.name\s*=\s*["']([^"']+)["']""")
        if gem:
            break
    if name.startswith("rust-") or (cargo and not c_build):
        return "rust", cargo or name.removeprefix("rust-")
    if name.startswith("python3-"):
        return "python", pypi or name.removeprefix("python3-")
    if name.startswith("python-"):
        return "python", pypi or name.removeprefix("python-")
    if (pypi or (root / "setup.py").is_file()) and not c_build:
        return "python", pypi or name
    if name.startswith("rubygem-") or gem:
        return "ruby", gem or name.removeprefix("rubygem-")
    if name.startswith("nodejs-") or "registry.npmjs.org" in url_l:
        return "node", name.removeprefix("nodejs-")
    if name.startswith("perl-") or "metacpan.org" in url_l or "cpan.org" in url_l:
        src = name.removeprefix("perl-")
        return "perl", src.replace("-", "::")
    if name.startswith("R-") and name not in {"R"}:
        return "r", name[2:]
    if name.startswith("ghc-") and name not in {"ghc", "ghc-rpm-macros"}:
        return "haskell", name.removeprefix("ghc-")
    if name.startswith("php-"):
        return "php", name.removeprefix("php-")
    if gomod and not c_build:
        return "go", gomod
    if name.startswith("golang-") or name.startswith("go-"):
        return "go", gomod or name
    return None


def oreonize_translator_spec(text: str, source_hash: str, source_url: str, version: str) -> str:
    try:
        from .orbscli import _split_spec_changelog
    except ImportError:
        from orbscli import _split_spec_changelog
    pre, _log = _split_spec_changelog(text)
    pre = pre.replace("# !!!ADD DEPS!!! !!!ADD DEPS!!! !!!ADD DEPS!!!\n", "")
    source = templated_source(source_url, version)
    if re.search(r"(?im)^\s*Source\d*\s*:", pre):
        pre = re.sub(
            r"(?im)^(\s*Source0?\s*:\s*)\S.*$",
            lambda m: m.group(1) + source,
            pre,
            count=1,
        )
    else:
        pre = pre.rstrip() + "\nSource0: %s\n" % source
    if not re.search(r"(?im)^[ \t]*%global\s+source0_hash\b", pre):
        pre = "%%global source0_hash %s\n" % source_hash + pre
    if "debug_package" not in pre:
        pre = pre.replace(
            "%%global source0_hash %s\n" % source_hash,
            "%%global source0_hash %s\n%%global debug_package %%{nil}\n" % source_hash,
            1,
        )
    needle = "%global debug_package %{nil}\n"
    if "oreon_bootstrap" not in pre and needle in pre:
        pre = pre.replace(
            needle,
            needle + "%global oreon_bootstrap %{nil}\n%bcond bootstrap 0\n%oreon_bootstrap\n",
            1,
        )
    if "oreon-rpm-macros" not in pre:
        pre = re.sub(
            r"(?im)^(BuildRequires\s*:)",
            "BuildRequires: oreon-rpm-macros\n\\1",
            pre,
            count=1,
        )
    if "%oreon_sha256" not in pre and re.search(r"(?m)^%prep\b", pre):
        pre = re.sub(r"(?m)^%prep\b", "%prep\n%oreon_sha256", pre, count=1)
    if not pre.endswith("\n"):
        pre += "\n"
    return pre


def translator_spec(name: str, version: str, source_url: str, source_hash: str, root: Path) -> str | None:
    found = lang_kind_from_tree(root, name, source_url)
    if not found:
        return None
    kind, upstream = found
    if not upstream:
        return None
    try:
        from .orbscli import _lang2rpm_generate
    except ImportError:
        from orbscli import _lang2rpm_generate
    body, err = _lang2rpm_generate(kind, upstream, version, name)
    if not body:
        click.echo("%s translator failed (%s)" % (kind, err or "no spec"), err=True)
        return None
    click.echo("deps from %s" % kind, err=True)
    return oreonize_translator_spec(body, source_hash, source_url, version)


def add_spec(
    name: str,
    source_url: str | None,
    version: str | None,
    spec_root: Path,
    force: bool = False,
    dry_run: bool = False,
) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
        raise click.ClickException("bad package name")
    dest = spec_root / name / ("%s.spec" % name)
    if dest.is_file() and not force:
        raise click.ClickException("%s already exists" % dest)
    if not source_url:
        raise click.ClickException("pass --url, a tarball or a github repo")
    refuse_lookaside(source_url)
    if re.search(r"github\.com/[^/]+/[^/]+/?$", source_url) and not re.search(r"\.(tar|tgz|zip|crate)", source_url):
        source_url, version = github_tarball(source_url, version)
        refuse_lookaside(source_url)
    if not version:
        version = version_from_filename(source_url)
    if not version:
        raise click.ClickException("pass --version, the tarball name did not have one")
    import httpx

    fetch, unpack = fetch_helpers()
    with httpx.Client(timeout=120.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        got = fetch(client, source_url, None)
    if not got:
        raise click.ClickException("could not download %s" % source_url)
    archive, digest = got
    import tempfile

    try:
        with tempfile.TemporaryDirectory(prefix="orbscli-spec-add-") as raw:
            tree = unpack(archive, Path(raw) / "src")
            if tree is None:
                raise click.ClickException("could not unpack %s" % source_url)
            text = translator_spec(name, version, source_url, digest, tree)
            if not text:
                info = inspect_tree(tree)
                if info.get("version") and info["version"] != version and "%{" not in info["version"]:
                    version = info["version"]
                text = render_spec(
                    name,
                    version,
                    source_url,
                    digest,
                    info,
                    topdir_macro(tree, name, version),
                )
    finally:
        archive.unlink(missing_ok=True)
    if "src.fedoraproject.org" in text or "QA_RPATHS" in text or "auto_features" in text:
        raise click.ClickException("refusing to write a spec that breaks the packaging rules")
    if dry_run:
        click.echo(text)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    click.echo(str(dest))
    return dest


def register(cli) -> None:
    @cli.command("spec-add")
    @click.argument("name")
    @click.option("--url", default=None, help="Upstream tarball, or a github repo.")
    @click.option("--version", default=None, help="Upstream version. Taken from the tarball name when it is there.")
    @click.option("--spec-root", type=click.Path(file_okay=False, path_type=Path), default=None)
    @click.option("--force", is_flag=True, default=False, help="Overwrite an existing spec.")
    @click.option("--dry-run", is_flag=True, default=False, help="Print the spec and do not write it.")
    def cmd_spec_add(name, url, version, spec_root, force, dry_run):
        """Write a new spec from the upstream tarball."""
        root = (spec_root or default_spec_root()).expanduser()
        if not root.is_dir():
            raise click.ClickException("spec root is not a directory %s" % root)
        add_spec(name, url, version, root, force=force, dry_run=dry_run)
