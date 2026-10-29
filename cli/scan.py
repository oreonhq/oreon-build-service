# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import multiprocessing
import re
import threading
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

try:
    from . import planner as P
except ImportError:
    import planner as P

FIXED = {
    "_prefix": "/usr", "_usr": "/usr", "_bindir": "/usr/bin", "_sbindir": "/usr/sbin",
    "_libdir": "/usr/lib64", "_lib": "lib64", "_libexecdir": "/usr/libexec", "_datadir": "/usr/share",
    "_datarootdir": "/usr/share", "_includedir": "/usr/include", "_sysconfdir": "/etc",
    "_mandir": "/usr/share/man", "_docdir": "/usr/share/doc", "_localstatedir": "/var",
    "_unitdir": "/usr/lib/systemd/system", "_texmf_main": "/usr/share/texlive/texmf-dist",
    "_texmf_dist": "/usr/share/texlive/texmf-dist", "python3_sitelib": "/usr/lib/python3.14/site-packages",
    "python3_sitearch": "/usr/lib64/python3.14/site-packages", "python3_version": "3.14",
    "python3_pkgversion": "3", "_isa": "",
    "perl_vendorlib": "/usr/share/perl5/vendor_perl", "perl_vendorarch": "/usr/lib64/perl5/vendor_perl",
    "perl_privlib": "/usr/share/perl5", "perl_archlib": "/usr/lib64/perl5",
}

_MACRO = re.compile(r"%\{(\??)(\w+)\}|%(\w+)")
_DEFINE = re.compile(r"(?m)^%(?:global|define)\s+(\w+)\s+(.*?)\s*$")
_OPS = {"=", ">=", "<=", "<", ">"}


def expand(text, macros):
    for _ in range(6):
        def sub(match):
            key = match[2] or match[3]
            if key in macros:
                return macros[key]
            return "" if match[1] else match[0]
        new = _MACRO.sub(sub, text)
        if new == text:
            break
        text = new
    return text


_FILE_MACRO = re.compile(r"(?m)^%(?:global\s+|define\s+)?(\w+)[ \t]+(\S[^\n]*?)[ \t]*$")
HOST = {}


GENERATOR = re.compile(
    r"(?m)^%(?:pyproject_extras_subpkg|python_extras_subpkg|ghc_lib_subpackage|ghc_pkg\w*|fontpkg|gopkg|gometa|"
    r"mingw_package|ocaml_\w*package|jpackage\w*)\b")


def tree_macros(root):
    out = {}
    for path in sorted(Path(root).glob("*/macros.*")) + sorted(Path(root).glob("*/*.macros")):
        if path.suffix == ".in":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _FILE_MACRO.finditer(text):
            value = match[2]
            if not value.endswith("\\") and not value.startswith(("(", "{")):
                out.setdefault(match[1], value)
    return out


def macro_index(root, light):
    out = {}
    for path in sorted(Path(root).glob("*/macros.*")):
        if path.suffix == ".in" or path.parent.name not in light:
            continue
        for binary in light[path.parent.name].binaries:
            if not re.search(r"macros|filesystem|rpm-config", binary["name"]):
                continue
            out.setdefault(binary["name"], [])
            if str(path) not in out[binary["name"]]:
                out[binary["name"]].append(str(path))
    return out


def host_macros(loads=()):
    files = [Path("/usr/lib/rpm/macros"), Path("/usr/lib/rpm/redhat/macros")]
    for folder in ("/usr/lib/rpm/macros.d", "/etc/rpm"):
        files += sorted(Path(folder).glob("macros*")) if folder == "/etc/rpm" else sorted(Path(folder).iterdir())
    files += [Path(load) for load in loads]
    out = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _FILE_MACRO.finditer(text):
            value = match[2]
            if value.endswith("\\") or value.startswith(("{", "(")) and len(value) > 1 and value[0] != "%":
                continue
            out[match[1]] = value
    out.update(FIXED)
    return out


def scan_spec(root, name):
    path = Path(root) / name / (name + ".spec")
    spec = P.Spec(name, str(path))
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        spec.error = "cannot read spec: " + str(exc)
        return spec.to_json()
    spec.flags = P.spec_flags(raw)
    deps_file = path.parent / "chain.deps"
    if deps_file.is_file():
        for line in deps_file.read_text(encoding="utf-8", errors="replace").splitlines():
            item = line.split("#", 1)[0].strip()
            if item:
                spec.chain_deps.append(item)
    macros = dict(HOST or FIXED)
    seen_values = defaultdict(set)
    for match in _DEFINE.finditer(raw):
        macros[match[1]] = match[2]
        seen_values[match[1]].add(match[2])
    ambiguous = {key for key, values in seen_values.items() if len(values) > 1}
    found = re.search(r"(?m)^Name:\s*(.+?)\s*$", raw)
    rpm_name = expand(found[1], macros) if found else name
    if "%" in rpm_name or not rpm_name:
        rpm_name = name
    macros["name"] = rpm_name
    version = re.search(r"(?m)^Version:\s*(.+?)\s*$", raw)
    if version:
        macros["version"] = expand(version[1], macros)
    epoch = re.search(r"(?m)^Epoch:\s*(.+?)\s*$", raw)
    if epoch:
        macros["epoch"] = expand(epoch[1], macros)
    release = re.search(r"(?m)^Release:\s*(.+?)\s*$", raw)
    if release:
        rel = expand(release[1], macros).split("%")[0].strip()
        if rel:
            macros["release"] = rel
    spec.rpm_name = rpm_name
    sources = {}
    for match in re.finditer(r"(?im)^Source(\d+):\s*(.+?)\s*$", raw):
        val = expand(match[2], macros)
        if "%" not in val:
            sources["SOURCE" + match[1]] = val
    chase, used = [found[1] if found else ""], set()
    for _ in range(4):
        nxt = []
        for text in chase:
            for key in re.findall(r"%\{?\??(\w+)", text):
                if key not in used:
                    used.add(key)
                    nxt.append(macros.get(key, ""))
        chase = nxt
    provides = defaultdict(list)
    packages = [rpm_name]
    current = rpm_name
    for line in raw.split("\n"):
        if line.startswith("%changelog"):
            break
        if line.startswith("%package"):
            sub = P.binary_name(expand(line[8:], macros), rpm_name)
            if "%" not in sub:
                current = sub
                if sub not in packages:
                    packages.append(sub)
            else:
                current = None
            continue
        if line.startswith("%py_provides") and current:
            for token in expand(line[12:], macros).split():
                if "%" not in token:
                    provides[current].append([token, "", ""])
            continue
        if line.startswith(("%mvn_artifact", "%mvn_file", "%mvn_alias", "%mvn_compat_version")) and current:
            exp = expand(line, {**macros, **sources})
            if "SOURCE" in line:
                exp = re.sub(r"%\{?\??SOURCE\d+\}?", "", exp)
            for pom in re.finditer(r"https?://repo[0-9]*\.maven\.org/maven2/(\S+)", exp):
                parts = [part for part in pom[1].split("/") if part]
                if len(parts) > 3 and re.search(r"\d", parts[-2]) and parts[-1].startswith(parts[-3] + "-"):
                    provides[current].append(["mvn(" + ".".join(parts[:-3]) + ":" + parts[-3] + ")", "", ""])
            if line.startswith("%mvn_compat_version"):
                tokens = exp.split()
                if len(tokens) >= 2 and re.fullmatch(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.+-]+", tokens[1]):
                    base = tokens[1]
                    provides[current].append(["mvn(" + base + ")", "", ""])
                    for extra in tokens[2:]:
                        if extra and "%" not in extra:
                            provides[current].append(["mvn(" + base + ":" + extra + ")", "", ""])
                    continue
            for token in exp.split()[1:]:
                if re.fullmatch(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.+-]+(:[A-Za-z0-9_.+-]+)*:?", token.rstrip(":")) and token.count(":") >= 1:
                    provides[current].append(["mvn(" + token.rstrip(":") + ")", "", ""])
                stem = token.rsplit("/", 1)[-1]
                ver = macros.get("version") or ""
                if stem.endswith(".pom") and ver and stem.endswith("-" + ver + ".pom"):
                    provides[current].append(["mvn-artifact(" + stem[:-len(ver) - 5] + ")", "", ""])
            continue
        if re.match(r"(?i)nosource\d+|source\d+", line) and current:
            exp = expand(line, {**macros, **sources})
            for pom in re.finditer(r"https?://repo[0-9]*\.maven\.org/maven2/(\S+)", exp):
                parts = [part for part in pom[1].split("/") if part]
                if len(parts) > 3 and re.search(r"\d", parts[-2]) and parts[-1].startswith(parts[-3] + "-"):
                    provides[rpm_name].append(["mvn(" + ".".join(parts[:-3]) + ":" + parts[-3] + ")", "", ""])
                    provides[rpm_name].append(["mvn-artifact(" + parts[-3] + ")", "", ""])
            continue
        if line.startswith("%mvn_package"):
            exp = expand(line, {**macros, **sources})
            tokens = exp.split()
            if len(tokens) >= 2:
                filt = tokens[1]
                aid = filt[1:] if filt.startswith(":") else (filt.split(":")[1] if filt.count(":") >= 1 else "")
                if re.fullmatch(r"[A-Za-z0-9_.+-]+", aid):
                    provides[rpm_name].append(["mvn-artifact(" + aid + ")", "", ""])
            continue
        if line[:9].lower() == "provides:" and current:
            for piece in expand(line[9:], macros).split(","):
                tokens = piece.split()
                if not tokens or "%" in tokens[0]:
                    continue
                if len(tokens) >= 3 and tokens[1] in _OPS:
                    provides[current].append([tokens[0], tokens[1], tokens[2]])
                else:
                    provides[current].append([tokens[0], "", ""])
    text = []
    for section in P._RE_SECTION.finditer(raw):
        target = P.binary_name(expand(section[1], macros), rpm_name)
        if "%" in target:
            continue
        body = "\n".join(expand(line, macros) for line in section[2].split("\n"))
        text.append("%files -n " + target + "\n" + body + "\n")
    files = P.files_provides("".join(text), rpm_name, "x86_64") if text else {}
    version = macros.get("version", "")
    version = version if version and "%" not in version else "\x00"
    epoch = macros.get("epoch", "0")
    epoch = epoch if epoch and "%" not in epoch else "0"
    spec.binaries = [{"name": item, "evr": [epoch, version, ""], "provides": provides.get(item, []),
                      "files": sorted(files.get(item, ()))} for item in packages]
    names = []
    for match in re.finditer(r"(?im)^BuildRequires:\s*(.+)$", raw):
        for token in re.split(r"[,\s]+", match[1]):
            if token and re.fullmatch(r"[A-Za-z0-9._+()%{}:-]+", token):
                names.append(token)
    spec.brs = {"x86_64": names}
    spec.dynamic = bool(re.search(r"(?m)^%generate_buildrequires\b", raw))
    spec.arches = [arch for arch in ("x86_64", "aarch64") if P.arch_applies(raw, arch)]
    unexpanded = any("%" in expand(m[1], macros) for m in re.finditer(r"(?m)^%package\s+(?:-n\s+)?(\S+)", raw))
    if GENERATOR.search(raw) or used & ambiguous or unexpanded:
        spec.flags.append("generated")
    return spec.to_json()


def _scan_chunk(job):
    root, names = job
    return [scan_spec(root, name) for name in names]


def scan_all(root, names, arches, jobs=8, loads=()):
    HOST.clear()
    HOST.update(tree_macros(root))
    HOST.update(host_macros(loads))
    size = max(1, len(names) // (jobs * 8))
    chunks = [(str(root), names[index:index + size]) for index in range(0, len(names), size)]
    out = {}
    with ProcessPoolExecutor(max_workers=jobs, mp_context=multiprocessing.get_context("fork")) as pool:
        for part in pool.map(_scan_chunk, chunks):
            for data in part:
                spec = P.Spec.from_json(data)
                spec.arches = [arch for arch in arches if arch in spec.arches] or list(arches)
                out[spec.name] = spec
    return out


class SpecStore:
    def __init__(self, tool, light, arches):
        self.tool = tool
        self.light = light
        self.arches = list(arches)
        self.full = {}
        self.quick = {}
        self.exact = set()
        self.lock = threading.Lock()

    def __contains__(self, name):
        return name in self.light

    def __len__(self):
        return len(self.light)

    def __iter__(self):
        return iter(self.light)

    def names(self):
        return list(self.light)

    def peek(self, name):
        return self.full.get(name) or self.light[name]

    def get(self, name, default=None):
        return self.peek(name) if name in self.light else default

    def values(self):
        return [self.peek(name) for name in self.light]

    def items(self):
        return [(name, self.peek(name)) for name in self.light]

    def load(self, name):
        spec = self.full.get(name)
        if spec is None:
            head = None
            quick = self.quick.get(name)
            if quick and quick[0] is not None:
                head = quick[3]
            spec = P.read_spec(self.tool, name, self.arches, head, self.light.get(name))
            self.full[name] = spec
        return spec

    def __getitem__(self, name):
        if name not in self.light:
            raise KeyError(name)
        return self.load(name)

    def exactify(self, jobs):
        todo = [name for name, spec in self.light.items() if "generated" in spec.flags and name not in self.exact]
        self.exact.update(todo)
        def one(name):
            light = self.light[name]
            try:
                out = self.tool.run("x86_64", Path(light.path), ["-q", "--qf", P._FMT])
            except (P.PlanError, OSError, ValueError):
                return None
            found = {}
            for record in out.split("\x1e"):
                if not record.strip():
                    continue
                head, _, tail = record.partition("\x1f")
                bname = head.strip().split("\t")[0]
                item = found.setdefault(bname, {"name": bname, "evr": None, "provides": [], "files": []})
                for line in tail.split("\x1d"):
                    parts = line.split()
                    if parts:
                        entry = [parts[0], parts[1] if len(parts) > 2 else "", parts[2] if len(parts) > 2 else ""]
                        if entry not in item["provides"]:
                            item["provides"].append(entry)
            old = {item["name"]: item for item in light.binaries}
            for bname, item in found.items():
                if bname in old:
                    item["files"] = old[bname]["files"]
            light.binaries = list(found.values()) or light.binaries
            return light
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            return [spec for spec in pool.map(one, todo) if spec is not None]

    def load_many(self, names, jobs):
        todo = [name for name in names if name in self.light and name not in self.full]
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(self.load, todo))

    def evr(self, name):
        if name not in self.quick:
            light = self.light[name]
            try:
                head = P.read_head(self.tool, Path(light.path))
                self.quick[name] = (head[0], head[1], "", head)
            except (P.PlanError, OSError, ValueError) as exc:
                self.quick[name] = (None, "", str(exc) or exc.__class__.__name__, None)
        return self.quick[name][:3]

    def deepen_many(self, names, jobs):
        todo = [self.load(name) for name in names if name in self.light]
        todo = [spec for spec in todo if not spec.error and not spec.sources]
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(lambda spec: P.deepen(self.tool, spec), todo))

    def prefetch(self, names, jobs):
        todo = [name for name in names if name in self.light and name not in self.quick]
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(self.evr, todo))

    def done(self, name, repos):
        full = self.full.get(name)
        if full is not None:
            return P.published(full, repos)
        evr, rpm_name, error = self.evr(name)
        if evr is None:
            return False
        head = self.quick[name][3]
        arches = [arch for arch in self.arches if P.arch_ok(arch, head[2], head[3])] or self.arches
        return all(evr in repos[arch].sources.get(rpm_name, ()) for arch in arches)
