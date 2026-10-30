# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Fast dependency planner for the Oreon spec tree.

Static BuildRequires come from rpmspec and are cached per spec hash.
Repo state comes from primary.xml and is cached per repomd checksum.
Only generated BuildRequires (pyproject, cargo, go, R) look at source archives.
"""
from __future__ import annotations

import fnmatch
import functools
import gzip
import hashlib
import inspect
import json
import lzma
import os
import pickle
import re
import sys
import shlex
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

try:
    import rpm
except ImportError:
    rpm = None

try:
    from . import cabal as _cabal
except ImportError:
    import cabal as _cabal

BOOT = "#boot"
ARCH_SENSITIVE = re.compile(
    r"%ifn?arch|%\{_arch\}|%\{\??_isa\}|ExclusiveArch|ExcludeArch|%\{ix86\}|%\{arm\}|%\{power64\}|"
    r"%\{java_arches\}|%\{rust_arches\}|%\{ocaml_native_compiler\}|%\{golang_arches\}|%\{_target_cpu\}|"
    r"%\{mono_arches\}|%\{nodejs_arches\}|%\{valgrind_arches\}|%\{ghc_arches\}|%\{!?\??_with")


class PlanError(RuntimeError):
    pass


def evr_tuple(text):
    text = (text or "").strip()
    epoch = "0"
    if ":" in text:
        head, rest = text.split(":", 1)
        if head.isdigit():
            epoch, text = head, rest
    version, _, release = text.partition("-")
    return (epoch, version, release)


def evr_compare(have, want):
    if rpm is None:
        raise PlanError("the rpm python module is required")
    if have is None or want is None:
        if have is None and want is None:
            return 0
        return -1 if have is None else 1
    release_a = have[2] if have[2] and want[2] else "0"
    release_b = want[2] if have[2] and want[2] else "0"
    return rpm.labelCompare((str(have[0]), have[1], release_a), (str(want[0]), want[1], release_b))


def op_holds(order, op):
    if op in ("=", "=="):
        return order == 0
    if op == ">=":
        return order >= 0
    if op == ">":
        return order > 0
    if op == "<=":
        return order <= 0
    if op == "<":
        return order < 0
    return True


def provide_holds(entry, op, want):
    have_op, have = entry[0], entry[1]
    if not op or not have_op or have is None or have[1] == "\x00":
        return True
    if have_op in ("=", "=="):
        return op_holds(evr_compare(have, want), op)
    return True


_ATOM = re.compile(r"^(\S+?)(?:\s*(<=|>=|==|=|<|>)\s*(\S+))?$")
_ISA = re.compile(r"\((?:x86-64|aarch-64|x86-32|ppc-64|s390-64|riscv-64)\)$")


def split_atom(text):
    found = _ATOM.match(text.strip())
    if not found:
        return text.strip(), None, None
    name, op, version = found.groups()
    return name, op, (evr_tuple(version) if version else None)


def tokenize_rich(text):
    tokens, index, size = [], 0, len(text)
    while index < size:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == "(":
            tokens.append("(")
            index += 1
            continue
        if char == ")":
            tokens.append(")")
            index += 1
            continue
        start, depth = index, 0
        while index < size:
            char = text[index]
            if char.isspace() and depth == 0:
                break
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            index += 1
        tokens.append(text[start:index])
    return tokens


def parse_rich(text):
    tokens = tokenize_rich(text)
    position = 0

    def group():
        nonlocal position
        items, ops, current = [], [], []
        while position < len(tokens):
            token = tokens[position]
            position += 1
            if token == "(":
                current.append(group())
            elif token == ")":
                break
            elif token in ("and", "or", "if", "unless", "with", "without", "else"):
                items.append(current)
                ops.append(token)
                current = []
            else:
                current.append(token)
        items.append(current)
        return ("group", items, ops)

    if not tokens or tokens[0] != "(":
        return ("atom", text.strip())
    position = 1
    return group()


def _flatten(item):
    if len(item) == 1 and isinstance(item[0], tuple):
        return item[0]
    parts = []
    for part in item:
        parts.append(part if isinstance(part, str) else "")
    return ("atom", " ".join(parts))


@dataclass
class Resolution:
    repo_ok: bool = True
    owners: set = field(default_factory=set)
    unresolved: set = field(default_factory=set)

    def merge(self, other):
        self.repo_ok = self.repo_ok and other.repo_ok
        self.owners |= other.owners
        self.unresolved |= other.unresolved


def _conjunction_clauses(node):
    kind = node[0]
    if kind == "atom":
        name, op, want = split_atom(node[1])
        if not name or name.startswith("%"):
            return None
        return [(name, op, want)]
    _, items, ops = node
    if not ops or any(op not in ("with", "and") for op in ops):
        return None
    clauses = []
    for item in items:
        if not item:
            return None
        part = _conjunction_clauses(_flatten(item))
        if part is None:
            return None
        clauses.extend(part)
    if len({name for name, _op, _want in clauses}) != 1:
        return None
    return clauses


def eval_rich(node, atom_fn, text):
    kind = node[0]
    if kind == "atom":
        return atom_fn(node[1])
    _, items, ops = node
    children = [eval_rich(_flatten(item), atom_fn, text) if item else Resolution() for item in items]
    if not ops:
        return children[0]
    if "or" in ops:
        for child in children:
            if child.repo_ok and not child.unresolved:
                return Resolution()
        for child in children:
            if child.owners and not child.unresolved:
                return Resolution(False, set(child.owners), set())
        return Resolution(False, set(), {text})
    if "if" in ops or "unless" in ops or "with" in ops or "without" in ops or "else" in ops:
        return children[0]
    merged = Resolution()
    for child in children:
        merged.merge(child)
    return merged


class Repo:
    def __init__(self):
        self.provides = {}
        self.requires = {}
        self.sources = {}
        self.binaries = {}
        self.python = {}
        self.build_times = {}

    def satisfies(self, name, op, want):
        entries = self.provides.get(name)
        if not entries:
            return False
        if not op:
            return True
        return any(provide_holds(entry, op, want) for entry in entries)

    def installable(self, bname):
        if bname is None:
            return True
        memo = getattr(self, "_inst_memo", None)
        if memo is None:
            memo = self._inst_memo = {}
            self._inst_busy = set()
        if bname in memo:
            return memo[bname]
        if bname in self._inst_busy:
            return True
        self._inst_busy.add(bname)
        result = True
        for rname, rop, rver in self.requires.get(bname, ()):
            if not any(provide_holds(entry, rop, rver) and (entry[2] == bname or self.installable(entry[2]))
                       for entry in self.provides.get(rname, ())):
                result = False
                break
        self._inst_busy.discard(bname)
        memo[bname] = result
        return result

    def satisfies_ok(self, name, op, want):
        entries = self.provides.get(name)
        if not entries:
            return False
        return any(provide_holds(entry, op, want) and self.installable(entry[2] if len(entry) > 2 else None)
                   for entry in entries)


_NS = {"c": "http://linux.duke.edu/metadata/common", "r": "http://linux.duke.edu/metadata/rpm",
       "m": "http://linux.duke.edu/metadata/repo"}
_SRC = re.compile(r"^(.+)-([^-]+)-([^-]+)\.src\.rpm$")
_FLAGS = {"EQ": "=", "GE": ">=", "LE": "<=", "GT": ">", "LT": "<"}


def _fetch(url, timeout=120):
    request = urllib.request.Request(url, headers={"User-Agent": "orbscli-planner"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _open_primary(blob, name, directory):
    if name.endswith(".zst"):
        path = Path(directory) / "primary.zst"
        path.write_bytes(blob)
        process = subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE)
        return process.stdout, process
    if name.endswith(".gz"):
        path = Path(directory) / "primary.gz"
        path.write_bytes(blob)
        return gzip.open(path, "rb"), None
    if name.endswith(".xz"):
        path = Path(directory) / "primary.xz"
        path.write_bytes(blob)
        return lzma.open(path, "rb"), None
    path = Path(directory) / "primary.xml"
    path.write_bytes(blob)
    return open(path, "rb"), None


def parse_primary(stream):
    repo = Repo()
    intern = {}

    def keep(value):
        return intern.setdefault(value, value)

    tag = lambda space, name: "{%s}%s" % (_NS[space], name)
    for _event, element in ET.iterparse(stream, events=("end",)):
        if element.tag != tag("c", "package"):
            continue
        name = element.findtext(tag("c", "name"))
        version = element.find(tag("c", "version"))
        if not name or version is None:
            element.clear()
            continue
        evr = (version.get("epoch") or "0", version.get("ver"), version.get("rel"))
        name = keep(name)
        stamp = element.find(tag("c", "time"))
        built = int(stamp.get("build") or 0) if stamp is not None else 0
        fmt = element.find(tag("c", "format"))
        source = None
        if fmt is not None:
            srpm = fmt.findtext(tag("r", "sourcerpm")) or ""
            found = _SRC.match(srpm)
            source = keep(found.group(1)) if found else name
            repo.sources.setdefault(source, set()).add(evr)
            key = (source, evr)
            if built > repo.build_times.get(key, 0):
                repo.build_times[key] = built
            container = fmt.find(tag("r", "provides"))
            if container is not None:
                for entry in container.findall(tag("r", "entry")):
                    provided = keep(entry.get("name"))
                    flag = _FLAGS.get(entry.get("flags") or "")
                    have = (entry.get("epoch") or "0", entry.get("ver") or "", entry.get("rel") or "") if flag else None
                    repo.provides.setdefault(provided, []).append((flag, have, name))
            needs = fmt.find(tag("r", "requires"))
            if needs is not None:
                for entry in needs.findall(tag("r", "entry")):
                    wanted = entry.get("name")
                    if not wanted or wanted.startswith("("):
                        continue
                    flag = _FLAGS.get(entry.get("flags") or "")
                    have = (entry.get("epoch") or "0", entry.get("ver") or "", entry.get("rel") or "") if flag else None
                    repo.requires.setdefault(name, []).append((keep(wanted), flag, have))
            for path in fmt.findall(tag("c", "file")):
                if path.text:
                    repo.provides.setdefault(keep(path.text), []).append((None, None, name))
        repo.provides.setdefault(name, []).append(("=", evr, name))
        repo.binaries[name] = source or name
        if re.fullmatch(r"python3(?:\.\d+)?", name):
            repo.python[name.removeprefix("python")] = evr[1]
        element.clear()
    for entries in repo.provides.values():
        entries[:] = list(dict.fromkeys(entries))
    for entries in repo.requires.values():
        entries[:] = list(dict.fromkeys(entries))
    return repo


def load_repo(url, cache_dir, force=False):
    base = url.rstrip("/")
    root = ET.fromstring(_fetch(base + "/repodata/repomd.xml"))
    primary = next((d for d in root.findall("m:data", _NS) if d.get("type") == "primary"), None)
    if primary is None:
        raise PlanError("no primary metadata in " + url)
    href = primary.find("m:location", _NS).get("href")
    checksum = primary.find("m:checksum", _NS).text
    cache = Path(cache_dir) / "repo"
    cache.mkdir(parents=True, exist_ok=True)
    stored = cache / ("r2-" + hashlib.sha256(url.encode()).hexdigest()[:16] + "-" + checksum[:32] + ".pickle")
    if stored.is_file() and not force:
        try:
            with open(stored, "rb") as handle:
                return pickle.load(handle)
        except Exception:
            stored.unlink(missing_ok=True)
    for old in cache.glob(hashlib.sha256(url.encode()).hexdigest()[:16] + "-*.pickle"):
        old.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="orbscli-primary-") as folder:
        stream, process = _open_primary(_fetch(base + "/" + href, 600), href, folder)
        try:
            repo = parse_primary(stream)
        finally:
            stream.close()
            if process is not None:
                process.wait()
    temp = stored.with_suffix(".tmp")
    with open(temp, "wb") as handle:
        pickle.dump(repo, handle, protocol=5)
    temp.replace(stored)
    return repo


def merge_repos(repos):
    if len(repos) == 1:
        return repos[0]
    merged = Repo()
    for repo in repos:
        for key, entries in repo.provides.items():
            merged.provides.setdefault(key, []).extend(entries)
        for key, entries in repo.requires.items():
            merged.requires.setdefault(key, []).extend(entries)
        for key, values in repo.sources.items():
            merged.sources.setdefault(key, set()).update(values)
        merged.binaries.update(repo.binaries)
        merged.python.update(repo.python)
    return merged


_RE_SECTION = re.compile(
    r"(?ms)^%files\b([^\n]*)\n(.*?)(?=^%(?:files|changelog|package|description|prep|build|install|check|pre|post|"
    r"preun|postun|pretrans|posttrans|triggerin|triggerun|triggerpostun|verifyscript|generate_buildrequires|conf)\b|\Z)")
_RE_DIRECTIVE = re.compile(r"^%(?:attr|config|verify|lang|defattr|caps|dir|doc|license|ghost|artifact)\b(?:\([^)]*\))?\s*")


def binary_name(arguments, source):
    try:
        words = shlex.split(arguments)
    except ValueError:
        return source
    position, subname = 0, None
    while position < len(words):
        word = words[position]
        if word == "-n" and position + 1 < len(words):
            return words[position + 1]
        if word in ("-f", "-s"):
            position += 2
            continue
        if not word.startswith("-") and subname is None:
            subname = word
        position += 1
    return source + "-" + subname if subname else source


def files_provides(expanded, source, arch):
    found = defaultdict(set)
    wide = "(64bit)" if arch in ("x86_64", "aarch64", "ppc64le", "s390x", "riscv64") else ""
    for section in _RE_SECTION.finditer(expanded):
        name = binary_name(section[1], source)
        for raw in section[2].splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("%exclude"):
                continue
            line = re.sub(r"%\{_texmf_(?:main|dist)\}", "/usr/share/texlive/texmf-dist", line)
            while True:
                stripped = _RE_DIRECTIVE.sub("", line, count=1)
                if stripped == line:
                    break
                line = stripped
            first = next((token for token in line.split() if token.startswith("/")), None)
            for path in brace_expand(first) if first else ():
                _file_provides(found, name, path, wide)
    return found


def brace_expand(path):
    match = re.search(r"\{([^{}%]*,[^{}%]*)\}", path)
    if not match:
        return [path]
    out = []
    for option in match[1].split(","):
        out += brace_expand(path[:match.start()] + option + path[match.end():])
    return out


def _file_provides(found, name, path, wide):
    if True:
        if True:
            if any(char in path for char in "%{}"):
                return
            literal = not any(char in path for char in "*?[]")
            if literal and not path.endswith("/"):
                found[name].add(path)
            pc = re.search(r"/pkgconfig/([^/*?\[\]]+)\.pc$", path)
            if pc:
                found[name].add("pkgconfig(%s)" % pc[1])
            pc_prefix = re.search(r"/pkgconfig/([A-Za-z0-9_.+-]+)\*\.pc$", path)
            if pc_prefix:
                found[name].add("pkgconfig(%s)" % pc_prefix[1])
            cmake_glob = re.search(r"/cmake/([A-Za-z0-9_+.-]+)\*/?$", path)
            if cmake_glob:
                found[name].add("glob:cmake(%s*)@%s" % (cmake_glob[1], name))
            pc_glob = re.search(r"/pkgconfig/([A-Za-z0-9_.+-]*)\*(?:\.pc)?$", path)
            if pc_glob:
                found[name].add("glob:pkgconfig(%s*)@%s" % (pc_glob[1], name))
            perl_mod = re.search(r"/perl5/(?:vendor_perl/)?([A-Za-z][A-Za-z0-9_]*(?:/[A-Za-z0-9_]+)*)\.pm$", path)
            if perl_mod and literal:
                found[name].add("perl(%s)" % perl_mod[1].replace("/", "::"))
            perl_dir = re.search(r"/perl5/(?:vendor_perl/)?([A-Z][A-Za-z0-9_]*)\*?/?$", path)
            if perl_dir:
                found[name].add("perldir(%s)" % perl_dir[1])
            tex_dir = re.search(r"/tex/[A-Za-z0-9_-]+/([A-Za-z0-9_.+-]+)/$", path)
            if tex_dir:
                found[name].add("texdir(%s)" % tex_dir[1])
            cmake = re.search(r"/([A-Za-z0-9_+.-]+?)(?:-c|C)onfig\.cmake$", path)
            if cmake:
                found[name].add("cmake(%s)" % cmake[1])
            directory = re.search(r"/cmake/([A-Za-z0-9_+.-]+)/?$", path)
            if directory and literal:
                found[name].add("cmake(%s)" % directory[1])
                bare = re.sub(r"-\d[\d.]*$", "", directory[1])
                if bare != directory[1]:
                    found[name].add("cmake(%s)" % bare)
            versioned = re.search(r"/cmake/([A-Za-z0-9_+.-]+?)-\*/?$", path)
            if versioned:
                found[name].add("cmake(%s)" % versioned[1])
            globbed = re.search(r"/cmake/([A-Za-z0-9_+.-]+)/(?:\*|[^/]*\*[^/]*\.cmake|[^/]*[Cc]onfig[^/]*\.cmake)$", path)
            if globbed:
                found[name].add("cmake(%s)" % globbed[1])
            soname = re.search(r"/(lib[A-Za-z0-9_+.-]*\.so\.\d+)$", path)
            if soname and literal:
                found[name].add(soname[1] + "()" + wide)
    return found


_RE_LINES = {
    "source": re.compile(r"(?im)^(Source\d*)\s*:\s*(\S+)"),
    "patch": re.compile(r"(?im)^(Patch\d*)\s*:\s*(\S+)"),
}


def arch_applies(expanded, arch):
    exclusive = re.findall(r"(?im)^ExclusiveArch\s*:\s*(.+)$", expanded)
    exclude = re.findall(r"(?im)^ExcludeArch\s*:\s*(.+)$", expanded)
    if exclusive:
        allowed = {token for line in exclusive for token in line.split()}
        if arch not in allowed:
            return False
    if exclude:
        blocked = {token for line in exclude for token in line.split()}
        if arch in blocked:
            return False
    return True


@dataclass
class Spec:
    name: str
    path: str = ""
    hash: str = ""
    evr: tuple | None = None
    arches: list = field(default_factory=list)
    brs: dict = field(default_factory=dict)
    binaries: list = field(default_factory=list)
    files: dict = field(default_factory=dict)
    dynamic: bool = False
    flags: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    patches: list = field(default_factory=list)
    hashes: dict = field(default_factory=dict)
    error: str = ""
    rpm_name: str = ""
    chain_deps: list = field(default_factory=list)
    loaded: bool = False

    def to_json(self):
        data = dict(self.__dict__)
        data["evr"] = list(self.evr) if self.evr else None
        return data

    @staticmethod
    def from_json(data):
        spec = Spec(data["name"])
        spec.__dict__.update(data)
        spec.evr = tuple(data["evr"]) if data.get("evr") else None
        return spec


class Tooling:
    """rpmspec access shared by index building and bcond retries."""

    def __init__(self, cli, root, loads, cache_dir):
        self.cli = cli
        self.root = Path(root)
        self.loads = list(loads)
        self.macro_index = {}
        self.brs_of = None
        self.cache_dir = Path(cache_dir)
        self.tmp = tempfile.gettempdir()

    def command(self, arch, spec, extra=()):
        command = ["rpmspec", *self.cli._rpmspec_distro_args(), *self.cli._rpmspec_target_args(arch),
                   "--define", "dist .or11", "--define", "_tmppath " + self.tmp,
                   "--define", "_sourcedir " + str(Path(spec).parent), *extra]
        for load in self.loads:
            command += ["--load", load]
        if self.brs_of is not None:
            seen = set(self.loads)
            for item in self.brs_of(Path(spec).parent.name):
                for path in self.macro_index.get(item, ()):
                    if path not in seen:
                        seen.add(path)
                        command += ["--load", path]
        return command

    def run(self, arch, spec, args, extra=()):
        result = subprocess.run([*self.command(arch, spec, extra), *args, str(spec)],
                                capture_output=True, text=True, timeout=180)
        if result.returncode:
            lines = [line for line in result.stderr.splitlines() if line.strip()]
            raise PlanError((lines[-1] if lines else "rpmspec failed")[:300])
        return result.stdout

    def expanded(self, arch, spec, extra=()):
        return self.run(arch, spec, ["-P"], extra)


_FMT = ("%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\x1f[%{PROVIDENAME} %{PROVIDEFLAGS:depflags} "
        "%{PROVIDEVERSION}\x1d]\x1e")


def spec_flags(raw):
    flags = []
    if re.search(r"(?m)^%bcond(?:_with|_without)?\s+bootstrap\b|%\{(?:with|without) bootstrap\}", raw):
        flags.append("bootstrap")
    if re.search(r"(?m)^%bcond(?:_with|_without)?\s+check\b|%\{(?:with|without) check\}", raw):
        flags.append("check")
    if "bootstrap" not in flags and re.search(r"%\{\??bootstrap\}|%\{!\?bootstrap\}|%if\s+.*%\{?\??bootstrap", raw):
        flags.append("macro_bootstrap")
    if "perl_bootstrap" in raw:
        flags.append("perl_bootstrap")
    for found in re.finditer(r"(?m)^%global\s+goipath\s+(\S+)", raw):
        flags.append("goipath:" + found[1])
    for found in re.finditer(r"(?m)^%bcond(?:_with|_without)?\s+((?:tests?|testsuite|check_tests|docs?|doc_build|manual|man|sphinx|typecheck|lint)\b)", raw):
        flag = "bcond:" + found[1]
        if flag not in flags:
            flags.append(flag)
    for found in re.finditer(r"(?m)^%bcond(?:_with|_without)?\s+(perl_\w+_enables_\w+)", raw):
        flag = "bcond:" + found[1]
        if flag not in flags:
            flags.append(flag)
    return flags


_HEAD_FMT = ("%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{NAME}\t[%{EXCLUSIVEARCH} ]\t[%{EXCLUDEARCH} ]\x1f"
             "[%{REQUIRENAME}\x1c%{REQUIREFLAGS:depflags}\x1c%{REQUIREVERSION}\x1d]")


def read_head(tool, path, arch="x86_64"):
    out = tool.run(arch, path, ["-q", "--srpm", "--qf", _HEAD_FMT])
    head, _, tail = out.partition("\x1f")
    fields = head.split("\t")
    if len(fields) != 6:
        raise PlanError("unexpected srpm header")
    brs = []
    for item in tail.split("\x1d"):
        parts = item.strip("\n").split("\x1c")
        if len(parts) != 3 or not parts[0] or parts[0].startswith("rpmlib("):
            continue
        name, flags, version = parts[0], parts[1].strip(), parts[2].strip()
        brs.append(" ".join([name, flags, version]) if version else name)
    return (fields[0], fields[1], fields[2]), fields[3], fields[4], fields[5], brs


def arch_ok(arch, exclusive, exclude):
    if exclusive.split() and arch not in exclusive.split():
        return False
    return arch not in exclude.split()


def read_spec(tool, name, arches, head=None, light=None):
    path = tool.root / name / (name + ".spec")
    spec = Spec(name, str(path))
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        spec.error = "cannot read spec: " + str(exc)
        return spec
    spec.flags = spec_flags(raw)
    try:
        if head is None:
            head = read_head(tool, path)
        spec.evr, spec.rpm_name = head[0], head[1]
        wide = [arch for arch in arches if arch_ok(arch, head[2], head[3])] or list(arches)
        spec.arches = wide
        sensitive = bool(ARCH_SENSITIVE.search(raw))
        binaries = {}
        for position, arch in enumerate(wide):
            if position and not sensitive:
                spec.brs[arch] = spec.brs[wide[0]]
                continue
            spec.brs[arch] = head[4] if arch == "x86_64" else read_head(tool, path, arch)[4]
            for record in tool.run(arch, path, ["-q", "--qf", _FMT]).split("\x1e"):
                if not record.strip():
                    continue
                entry_head, _, tail = record.partition("\x1f")
                bname, epoch, version, release = entry_head.strip().split("\t")
                item = binaries.setdefault(bname, {"name": bname, "evr": [epoch, version, release],
                                                   "provides": [], "files": []})
                for line in tail.split("\x1d"):
                    parts = line.split()
                    if not parts:
                        continue
                    entry = [parts[0], parts[1] if len(parts) > 2 else "", parts[2] if len(parts) > 2 else ""]
                    if entry not in item["provides"]:
                        item["provides"].append(entry)
        if light is not None:
            spec.chain_deps = list(light.chain_deps)
            for item in light.binaries:
                if item["name"] in binaries:
                    target = binaries[item["name"]]
                    target["files"] = sorted(set(target["files"]) | set(item["files"]))
                    for entry in item["provides"]:
                        if entry not in target["provides"]:
                            target["provides"].append(entry)
        spec.binaries = list(binaries.values())
        spec.dynamic = bool(re.search(r"(?m)^%generate_buildrequires\b", raw))
        spec.hashes = {m[1]: m[2] for m in re.finditer(r"(?m)^%global\s+source(\d+)_hash\s+([0-9a-f]{64})\s*$", raw)}
    except (PlanError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        spec.error = str(exc) or exc.__class__.__name__
        return spec
    spec.loaded = True
    return spec


def deepen(tool, spec):
    expanded = tool.expanded("x86_64", Path(spec.path))
    spec.sources = [[m[1], m[2]] for m in _RE_LINES["source"].finditer(expanded)]
    spec.patches = [[m[1], m[2]] for m in _RE_LINES["patch"].finditer(expanded)]


def list_spec_names(root):
    root = Path(root)
    return sorted(child.name for child in root.iterdir() if child.is_dir() and (child / (child.name + ".spec")).is_file())


def build_index(tool, names, arches, jobs=8):
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return dict(zip(names, pool.map(lambda item: read_spec(tool, item, arches), names)))


def alt_args(spec):
    args = []
    if "perl_bootstrap" in spec.flags:
        args += ["--define", "perl_bootstrap 1"]
    if "check" in spec.flags:
        args += ["--without", "check"]
    if "bootstrap" in spec.flags:
        args += ["--with", "bootstrap"]
    if "macro_bootstrap" in spec.flags:
        args += ["--define", "bootstrap 1"]
    for flag in spec.flags:
        if flag.startswith("bcond:"):
            args += ["--without", flag[6:]]
    return args


def _fuzzy_key(key):
    match = re.fullmatch(r"(cmake|pkgconfig)\((.+)\)", key)
    if not match:
        return ""
    return match[1] + ":" + re.sub(r"\d+$", "", match[2].lower())


def _norm(value):
    return re.sub(r"[^a-z0-9]+", "", value.lower())


class Resolver:
    def __init__(self, specs, repos, arches):
        self.specs = specs
        self.repos = repos
        self.arches = arches
        self.table = defaultdict(list)
        self.rust = defaultdict(list)
        self.binary_owner = {}
        self.weak = defaultdict(set)
        self.owned = defaultdict(list)
        self.fuzzy = defaultdict(list)
        self.gopaths = defaultdict(set)
        self.globs = defaultdict(list)
        for spec in specs.values():
            if spec.error:
                continue
            for binary in spec.binaries:
                self._add_binary(spec, binary)

    def _add(self, key, source, op, evr):
        if key.startswith("glob:"):
            body, _, base = key[5:].partition("@")
            self.globs[source].append((body, base, op or None, evr))
            return
        entry = (source, op or None, evr)
        if entry not in self.table[key]:
            self.table[key].append(entry)
        self.owned[source].append(("t", key))
        fuzzy = _fuzzy_key(key)
        if fuzzy:
            self.fuzzy[fuzzy].append(entry)
            self.owned[source].append(("f", fuzzy))

    def _add_binary(self, spec, binary):
        bname = binary["name"]
        evr = tuple(binary["evr"]) if binary.get("evr") else None
        exact = "=" if evr else None
        self.binary_owner[bname] = spec.name
        self.owned[spec.name].append(("b", bname))
        self._add(bname, spec.name, exact, evr)
        for name, op, version in binary["provides"]:
            self._add(_ISA.sub("", name), spec.name, op, evr_tuple(version) if op else None)
        for item in binary.get("files", ()):
            if evr and item.startswith(("cmake(", "pkgconfig(", "glob:cmake(", "glob:pkgconfig(")):
                self._add(item, spec.name, "=", evr)
            else:
                self._add(item, spec.name, None, None)
        python_names = [bname] + [item[0] for item in binary.get("provides", ())
                                  if item and item[0].startswith("python3-") and item[0] != bname and "(" not in item[0]]
        for python_name in python_names:
            python = re.fullmatch(r"python3(?:\.\d+)?-(.+)", python_name)
            if not python:
                continue
            dist, _, extra = python[1].partition("+")
            dist = re.sub(r"[-_.]+", "-", dist).lower()
            key = dist + ("[%s]" % extra.lower() if extra else "")
            for prefix in ("python3dist", "python3.14dist"):
                self._add("%s(%s)" % (prefix, key), spec.name, exact, evr)
                if not key.startswith("python-") and not extra:
                    self._add("%s(python-%s)" % (prefix, key), spec.name, exact, evr)
        crate = re.fullmatch(r"rust-(.+)-devel", bname)
        if crate:
            body = crate[1]
            stem, _, feature = body.partition("+")
            self.rust[stem].append((spec.name, bname, evr, feature))
            self.owned[spec.name].append(("r", stem))
            compats = set()
            compat = re.fullmatch(r"(.+?)(\d+(?:\.\d+)*)", stem)
            if compat:
                compats.add(compat[1])
            compat = re.fullmatch(r"(.+?)_([0-9]+(?:\.[0-9]+)+)", stem)
            if compat:
                compats.add(compat[1])
            for index in range(1, len(stem)):
                if re.fullmatch(r"\d+(?:\.\d+)+", stem[index:]) and not stem[index - 1] == "_":
                    compats.add(stem[:index])
            for base in compats:
                self.rust[base].append((spec.name, bname, evr, feature))
                self.owned[spec.name].append(("r", base))
        perl = re.fullmatch(r"perl-([A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*)", bname)
        if perl:
            self.weak["perl:" + perl[1]].add(spec.name)
            self.owned[spec.name].append(("w", "perl:" + perl[1]))
            self._add("perl(%s)" % perl[1].replace("-", "::"), spec.name, None, None)
        for stem in {re.sub(r"-devel$", "", bname)} if bname.endswith("-devel") else ():
            key = "dev:" + _norm(stem.removeprefix("lib"))
            self.weak[key].add(spec.name)
            self.owned[spec.name].append(("w", key))

    def refresh(self, spec):
        name = spec.name
        self.globs.pop(name, None)
        for kind, key in self.owned.pop(name, ()):
            if kind == "t":
                self.table[key] = [item for item in self.table[key] if item[0] != name]
            elif kind == "r":
                self.rust[key] = [item for item in self.rust[key] if item[0] != name]
            elif kind == "w":
                self.weak[key].discard(name)
            elif kind == "f":
                self.fuzzy[key] = [item for item in self.fuzzy[key] if item[0] != name]
            elif kind == "g":
                self.gopaths[key].discard(name)
            else:
                self.binary_owner.pop(key, None)
        if not spec.error:
            for binary in spec.binaries:
                self._add_binary(spec, binary)
            for flag in spec.flags:
                if flag.startswith("goipath:") and "%" not in flag:
                    self.gopaths[flag[8:]].add(name)
                    self.owned[name].append(("g", flag[8:]))

    def spec_providers(self, name, op, want):
        found = set()
        for source, have_op, have in self.table.get(name, ()):
            if provide_holds((have_op, have), op, want):
                found.add(source)
        crate = re.fullmatch(r"crate\(([^/)]+)(?:/([^)]+))?\)", name)
        if crate:
            stem, feature = crate.groups()
            options = [item for item in self.rust.get(stem, [])
                       if not op or item[2] is None or op_holds(evr_compare(item[2], want), op)]
            if feature:
                chosen = [item for item in options if item[3] == feature]
                if not chosen and feature == "default":
                    chosen = [item for item in options if not item[3] and not any(
                        other[0] == item[0] and other[3] == "default" for other in self.rust.get(stem, []))]
            else:
                chosen = [item for item in options if not item[3]]
            for source, _bname, _evr, _feature in chosen:
                found.add(source)
        golang = re.fullmatch(r"golang\((.+)\)", name)
        if golang and not found:
            best = max((path for path in self.gopaths if self.gopaths[path] and (golang[1] == path or golang[1].startswith(path + "/"))),
                       key=len, default=None)
            if best:
                found |= self.gopaths[best]
        mvn = re.fullmatch(r"mvn\(([^()]+)\)", name)
        if mvn and not found:
            parts = mvn[1].split(":")
            if len(parts) >= 2:
                for source, _mop, _mevr in self.table.get("mvn-artifact(" + parts[1] + ")", ()):
                    found.add(source)
            if not found:
                for arch in self.arches:
                    repo = self.repos[arch]
                    for entry in repo.provides.get(name, ()):
                        source = repo.binaries.get(entry[2] if len(entry) > 2 else None)
                        if source:
                            found.add(source)
        tex = re.fullmatch(r"tex\(([A-Za-z0-9_.+-]+?)\.(?:sty|cls|def|clo|cfg|tex|fd|bst|bbx|cbx|lbx)\)", name)
        if tex and not found:
            for source, _op, _evr in self.table.get("texdir(%s)" % tex[1], ()):
                found.add(source)
        if not found and name.startswith(("cmake(", "pkgconfig(")):
            key = _fuzzy_key(name)
            for source, have_op, have in self.fuzzy.get(key, ()):
                if provide_holds((have_op, have), op, want):
                    found.add(source)
            inner = name[name.index("(") + 1:-1]
            kind = name[:name.index("(") + 1]
            for source, patterns in self.globs.items():
                for pattern, base, have_op, have in patterns:
                    wild = pattern[len(kind):-1]
                    if not wild or wild == "*" or not pattern.startswith(kind) or not fnmatch.fnmatchcase(inner, wild):
                        continue
                    trimmed = re.sub(r"^(qt\d)-qt", r"\1", re.sub(r"-(devel|libs|static)$", "", base))
                    target = _norm(inner)
                    stems = {_norm(trimmed), _norm(trimmed.removeprefix("lib"))}
                    if any(len(stem) >= 3 and (stem in target or target in stem) for stem in stems) and provide_holds((have_op, have), op, want):
                        found.add(source)
        perl = re.fullmatch(r"perl\(([A-Za-z0-9_:]+)\)", name)
        if perl and not found and not perl[1].startswith(":"):
            parts = perl[1].split("::")
            dist = self._cpan_dist(perl[1])
            if dist:
                found |= self.weak.get("perl:" + dist, set())
            if not found:
                for length in range(len(parts), 0, -1):
                    owners = self.weak.get("perl:" + "-".join(parts[:length]))
                    if owners:
                        found |= owners
                        break
        if perl and not found:
            owners = sorted({source for source, _op, _evr in self.table.get("perldir(%s)" % perl[1].split("::")[0], ())}, key=lambda item: (len(item), item))
            if owners:
                found.add(owners[0])
        if len(found) > 1 and not name.startswith("mingw"):
            native = {source for source in found if not source.startswith("mingw-")}
            found = native or found
        return found

    def _cpan_dist(self, module):
        memo = self.__dict__.setdefault("_cpan_memo", {})
        if module not in memo:
            memo[module] = ""
            try:
                with urllib.request.urlopen(
                        "https://fastapi.metacpan.org/v1/module/%s?fields=distribution" % module, timeout=8) as reply:
                    memo[module] = json.load(reply).get("distribution", "").replace("::", "-")
            except Exception:
                pass
        return memo[module]

    def atom(self, text, arch):
        name, op, want = split_atom(text)
        if not name or name.startswith("%"):
            return Resolution()
        repo = self.repos[arch]
        if repo.satisfies_ok(name, op, want):
            return Resolution()
        owners = self.spec_providers(_ISA.sub("", name), op, want)
        if owners:
            return Resolution(False, owners, set())
        return Resolution(False, set(), {text.strip()})

    def requirement(self, text, arch):
        text = text.strip()
        if text.startswith("("):
            tree = parse_rich(text)
            if tree[0] == "group" and any(op in ("if", "unless") for op in tree[2]):
                branch, cond = _flatten(tree[1][0]), _flatten(tree[1][1] if len(tree[1]) > 1 else None)
                alt = None
                if "else" in tree[2] and len(tree[1]) > 2:
                    alt = _flatten(tree[1][2])
                cname, cop, cwant = split_atom(cond[1])
                repo = self.repos[arch]
                entries = [entry for entry in repo.provides.get(cname, ()) if entry[2] is not None]
                cond_true = False
                versioned = [entry for entry in entries if entry[1] is not None]
                if versioned:
                    best = max(versioned, key=functools.cmp_to_key(
                        lambda a, b: evr_compare(a[1], b[1])))
                    if best[0]:
                        cond_true = op_holds(evr_compare(best[1], cwant), cop)
                if cop == "=" and not cwant:
                    cond_true = bool(entries)
                if "unless" in tree[2]:
                    cond_true = not cond_true
                if not cond_true:
                    if alt is not None and alt[0] == "atom":
                        return self.requirement(alt[1], arch)
                    return Resolution()
                return self.requirement(branch[1], arch)
            clauses = _conjunction_clauses(tree)
            if clauses is not None:
                name = clauses[0][0]
                if self._repo_satisfies_all(name, clauses, arch):
                    return Resolution()
                owners = self._conjunction_providers(name, clauses)
                if owners:
                    return Resolution(False, owners, set())
                return Resolution(False, set(), {text})
            return eval_rich(tree, lambda value: self.atom(value, arch), text)
        return self.atom(text, arch)

    def _repo_satisfies_all(self, name, clauses, arch):
        repo = self.repos[arch]
        for entry in repo.provides.get(name, ()):
            if all(provide_holds(entry, op, want) for _n, op, want in clauses) and repo.installable(entry[2] if len(entry) > 2 else None):
                return True
        return False

    def _conjunction_providers(self, name, clauses):
        crate = re.fullmatch(r"crate\(([^/()]+)(?:/([^)]+))?\)", name)
        if crate:
            stem, feature = crate.groups()
            options = [item for item in self.rust.get(stem, []) if self._evr_holds_all(item[2], clauses)]
            if feature:
                chosen = [item for item in options if item[3] == feature]
            else:
                chosen = [item for item in options if not item[3]]
            if not chosen and feature == "default":
                chosen = [item for item in options if not item[3] and not any(
                    other[0] == item[0] and other[3] == "default" for other in self.rust.get(stem, []))]
            return {item[0] for item in chosen}
        owners = set()
        for source, have_op, have in self.table.get(name, ()):
            if self._provide_holds_all((have_op, have), clauses):
                owners.add(source)
        return owners

    def _evr_holds_all(self, evr, clauses):
        for _n, op, want in clauses:
            if op and evr is not None and not op_holds(evr_compare(evr, want), op):
                return False
        return True

    def _provide_holds_all(self, entry, clauses):
        for _n, op, want in clauses:
            if not provide_holds(entry, op, want):
                return False
        return True

    def suggest(self, text):
        name, _op, _want = split_atom(text)
        out = set()
        found = re.fullmatch(r"(?:pkgconfig|cmake)\(([^)]+)\)", name)
        if found:
            out |= self.weak.get("dev:" + _norm(found[1].removeprefix("lib")), set())
        found = re.fullmatch(r"python3(?:\.\d+)?dist\(([^)\[]+)", name)
        if found:
            candidate = "python-" + re.sub(r"[-_.]+", "-", found[1]).lower()
            if candidate in self.specs:
                out.add(candidate)
        found = re.fullmatch(r"rubygem\(([^)]+)\)", name)
        if found and "rubygem-" + found[1] in self.specs:
            out.add("rubygem-" + found[1])
        if name in self.specs:
            out.add(name)
        return sorted(out)


def published(spec, repos):
    if spec.error or spec.evr is None:
        return False
    for arch in spec.arches:
        if tuple(spec.evr) not in repos[arch].sources.get(spec.rpm_name or spec.name, ()):
            return False
    return True


def tarjan(graph):
    index, low, on_stack, stack, result = {}, {}, set(), [], []
    counter = [0]
    for start in sorted(graph):
        if start in index:
            continue
        work = [(start, iter(sorted(graph.get(start, ()))))]
        index[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in graph:
                    continue
                if child not in index:
                    index[child] = low[child] = counter[0]
                    counter[0] += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                result.append(sorted(component))
    return result


@dataclass
class Plan:
    layers: list = field(default_factory=list)
    blocked: dict = field(default_factory=dict)
    graph: dict = field(default_factory=dict)
    notes: dict = field(default_factory=dict)
    done: set = field(default_factory=set)
    unresolved: dict = field(default_factory=dict)
    warnings: dict = field(default_factory=dict)
    bootstrap: dict = field(default_factory=dict)
    f44: dict = field(default_factory=dict)

    @property
    def chain(self):
        return chain_text(self.layers)

    def names(self):
        return {show(name) for layer in self.layers for name in layer}


def show(name):
    return name.removesuffix(BOOT)


def chain_text(layers):
    return " : ".join(" ".join(show(name) for name in layer) for layer in layers)


SWITCH = "%oreon_bootstrap"


def has_switch(spec):
    try:
        text = Path(spec.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"(?m)^" + SWITCH + r"\s*$", text) is not None


def write_switch(spec):
    path = Path(spec.path)
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?ms)^%if %\{oreon_missing [^\n]*\n(?:%global [^\n]*\n)*%endif\n", "", text)
    if re.search(r"(?m)^" + SWITCH + r"\s*$", text):
        path.write_text(text, encoding="utf-8")
        return
    lines = text.split("\n")
    at = 0
    while at < len(lines) and re.match(r"%global\s+source\d+_hash\s", lines[at]):
        at += 1
    lines.insert(at, SWITCH)
    path.write_text("\n".join(lines), encoding="utf-8")


class Planner:
    def __init__(self, tool, specs, repos, arches, dynamic=True, jobs=8, progress=None):
        self.tool = tool
        self.specs = specs
        self.repos = repos
        self.arches = arches
        self.dynamic = dynamic
        self.jobs = jobs
        self.resolver = Resolver(specs, repos, arches)
        self.progress = progress or (lambda message: None)
        self.dynamic_cache = {}
        self.futures = {}
        self.dyn_pool = None
        self.escalated = False
        self.edge_arches = defaultdict(set)
        self.edge_reqs = defaultdict(set)
        self.f44_bootstrap = {}
        self.warnings = {}
        self.bootstrap = {}
        self.alt_cache = {}
        self.commit_cache = {}
        self.fresh_notes = {}
        self.force = set()

    def _spec_would_satisfy(self, name, owner):
        for text in self.edge_reqs.get((name, owner), ()):
            if text.startswith("("):
                parsed = parse_rich(text)
                clauses = _conjunction_clauses(parsed)
                if clauses is None:
                    return False
                cname = clauses[0][0]
                if owner not in self.resolver._conjunction_providers(cname, clauses):
                    return False
            else:
                cname, op, want = split_atom(text)
                if owner not in self.resolver.spec_providers(_ISA.sub("", cname), op, want):
                    return False
        return True

    def _commit_ts(self, name):
        try:
            result = subprocess.run(
                ["git", "-C", str(self.tool.root), "log", "-1", "--format=%ct", "--", name],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode == 0:
            value = result.stdout.strip()
            if value.isdigit():
                return int(value)
        return None

    def commit_ts(self, name):
        if name not in self.commit_cache:
            self.commit_cache[name] = self._commit_ts(name)
        return self.commit_cache[name]

    def stale(self, name):
        ts = self.commit_ts(name)
        if ts is None:
            return False
        evr, rpm_name, error = self.specs.evr(name)
        if error or evr is None:
            return False
        key = (rpm_name or name, evr)
        for arch in self.arches:
            built = self.repos[arch].build_times.get(key)
            if built is not None and built < ts:
                return True
        return False

    def dynamic_buildrequires(self, spec, arch):
        key = spec.name, arch
        if key in self.dynamic_cache:
            return self.dynamic_cache[key]
        try:
            from . import chain_metadata as metadata
        except ImportError:
            import chain_metadata as metadata
        cli = self.tool.cli
        path = Path(spec.path)
        expanded = self.tool.expanded(arch, path)
        binaries = [{"name": item["name"], "version": item["evr"][1], "provides": [], "requires": []}
                    for item in spec.binaries
                    if not (item["name"].startswith("rust-") and item["name"].endswith("-devel"))]
        cache = self.tool.cache_dir / "sources"
        value = metadata.evaluate(cli, path, arch, expanded, binaries, dict(self.repos[arch].python),
                                  cache / "any", cache / "verified")
        own = set()
        for item in [spec.name] + [binary["name"] for binary in spec.binaries]:
            item = re.sub(r"^(?:python3?(?:\.\d+)?-)", "", item)
            own.add(re.sub(r"[-_.]+", "-", item.split("+", 1)[0]).lower())

        def selfref(requirement):
            found = re.match(r"\(?\s*python3(?:\.\d+)?dist\(([^)\[]+)", requirement)
            return bool(found) and re.sub(r"[-_.]+", "-", found[1]).lower() in own and "(" not in requirement[:1]

        self.dynamic_cache[key] = [item for item in dict.fromkeys(value) if not selfref(item)]
        return self.dynamic_cache[key]

    def dynamic_all(self, spec):
        found, errors, warnings = {}, [], []
        for arch in spec.arches:
            for attempt in (0, 1):
                try:
                    found[arch] = self.dynamic_buildrequires(spec, arch)
                    break
                except OSError:
                    if attempt:
                        errors.append(arch + ": file error while reading sources")
                except Exception as exc:
                    message = (str(exc) or exc.__class__.__name__).strip().splitlines()[0][:300]
                    if message.startswith(("Unrecognized dependency generator", "Unsupported", "Multiple dependency")):
                        warnings.append("generated BuildRequires not evaluated, static ones only: " + message)
                    else:
                        errors.append(message)
                    break
        return found, list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))

    def own_requirements(self, spec, extra=(), dynamic=None):
        out = {}
        for arch in spec.arches:
            if extra:
                requirements = [line.strip() for line in self.tool.run(
                    arch, Path(spec.path), ["-q", "--buildrequires"], extra).splitlines() if line.strip()]
            else:
                requirements = list(spec.brs.get(arch, ()))
            if dynamic and arch in dynamic:
                requirements += dynamic[arch]
            out[arch] = requirements
        return out

    _dbg=False
    def _future_edge_ok(self, name, owner):
        if self.specs.done(owner, self.repos):
            return False
        evr, _rpm_name, error = self.specs.evr(owner)
        if error or evr is None:
            return False
        stem = owner[5:] if owner.startswith("rust-") else owner
        stem = stem[:-6] if stem.endswith("-devel") else stem
        have = evr
        for item in self.edge_reqs.get((name, owner), ()):
            for clause in item.split(" with "):
                cname, op, want = split_atom(clause.strip())
                found = re.match(r"crate\(([^/()]+)", cname or "")
                if not found or found[1] != stem or not op or want is None:
                    continue
                if not op_holds(evr_compare(have, want), op):
                    return False
        return True

    def resolve_spec(self, spec, requirements):
        owners, unresolved = set(), set()
        for arch, items in requirements.items():
            for item in items:
                result = self.resolver.requirement(item, arch)
                owners |= result.owners
                unresolved |= result.unresolved
                for owner in result.owners:
                    self.edge_arches[(spec.name, owner)].add(arch)
                    self.edge_reqs[(spec.name, owner)].add(item)
        owners.discard(spec.name)
        return owners, unresolved

    def prepare(self, name):
        store = self.specs
        if store.done(name, self.repos) and name not in self.force:
            if self.stale(name):
                self.fresh_notes[name] = "spec changed after the last published build, rebuilding to pick it up"
            else:
                return None
        spec = store.load(name)
        self.resolver.refresh(spec)
        if self.dynamic and spec.dynamic and not spec.error:
            self.futures[name] = self.dyn_pool.submit(self.dynamic_all, spec)
        return spec

    def plan(self, seeds):
        seeds = list(dict.fromkeys(seeds))
        self._pin_forced = []
        result = self._plan_once(seeds)
        for _round in range(3):
            extra = self._pin_forced
            if not extra:
                break
            self._pin_forced = []
            seeds = list(dict.fromkeys(seeds + extra))
            result = self._plan_once(seeds)
        return result

    def _plan_once(self, seeds):
        store = self.specs
        base = defaultdict(list)
        needed, seen, checked, done = [], set(), set(), set()
        for name in seeds:
            if name not in store:
                base[name].append("no spec for " + name)
            elif name not in seen:
                seen.add(name)
                needed.append(name)
        pool = ThreadPoolExecutor(max_workers=self.jobs)
        self.dyn_pool = ThreadPoolExecutor(max_workers=max(self.jobs * 4, 32))
        try:
            while True:
                pending = [name for name in needed if name not in checked]
                for name, result in zip(pending, pool.map(self.prepare, pending)):
                    checked.add(name)
                    if result is None:
                        done.add(name)
                graph, unresolved = {}, {}
                reasons = defaultdict(list, {key: list(value) for key, value in base.items()})
                fresh = []
                for name in needed:
                    if name in done:
                        continue
                    spec = store.load(name)
                    if spec.error:
                        graph[name] = set()
                        reasons[name].append("spec error: " + spec.error)
                        continue
                    extra = {}
                    if name in self.futures:
                        extra, errors, warned = self.futures[name].result()
                        for message in errors:
                            reasons[name].append("generated BuildRequires failed: " + message)
                        if warned:
                            self.warnings[name] = warned
                    owners, missing = self.resolve_spec(spec, self.own_requirements(spec, dynamic=extra))
                    for item in sorted(missing):
                        hints = self.resolver.suggest(item)
                        text = "nothing provides " + item
                        if hints:
                            text += " (spec with a matching name: " + ", ".join(hints) + ")"
                        reasons[name].append(text)
                        unresolved.setdefault(name, []).append(item)
                    graph[name] = set()
                    for owner in sorted(owners):
                        if owner in done:
                            if self._future_edge_ok(name, owner):
                                graph[name].add(owner)
                                continue
                            if self._spec_would_satisfy(name, owner):
                                done.discard(owner)
                                self.force.add(owner)
                                checked.discard(owner)
                                store.load(owner)
                                self.fresh_notes[owner] = ("published build of " + owner + " lacks provides the current spec generates, rebuilding")
                                graph[name].add(owner)
                                fresh.append(owner)
                                continue
                            wanted = ", ".join(sorted(self.edge_reqs[(name, owner)])[:4])
                            reasons[name].append("needs " + owner + " but the published build lacks " + wanted + " (spec changed without a Release bump or the spec claims a bogus provide)")
                        else:
                            graph[name].add(owner)
                            if owner not in seen:
                                seen.add(owner)
                                fresh.append(owner)
                    for dep in spec.chain_deps:
                        if dep == name or store.get(dep) is None:
                            continue
                        graph[name].add(dep)
                        if dep not in seen:
                            seen.add(dep)
                            fresh.append(dep)
                needed += fresh
                if not pending and not fresh:
                    if unresolved and hasattr(store, "exactify") and not self.escalated:
                        self.escalated = True
                        self.progress("Reading macro-generated subpackages")
                        for light in store.exactify(self.jobs):
                            self.resolver.refresh(store.peek(light.name))
                        continue
                    break
        finally:
            pool.shutdown(wait=True)
            self.dyn_pool.shutdown(wait=True)
        keep, queue = set(), deque(name for name in seeds if name in graph)
        while queue:
            name = queue.popleft()
            if name in keep:
                continue
            keep.add(name)
            queue.extend(dep for dep in graph.get(name, ()) if dep in graph)
        graph = {name: {dep for dep in deps if dep in keep} for name, deps in graph.items() if name in keep}
        reasons = defaultdict(list, {name: value for name, value in reasons.items() if name in keep or name in base})
        unresolved = {name: value for name, value in unresolved.items() if name in keep}
        self.done = done
        notes = {}
        self.split_cycles(graph, reasons, notes)
        for name, text in self.fresh_notes.items():
            if name in keep:
                notes[name] = text
        blocked = {name: list(dict.fromkeys(reason)) for name, reason in reasons.items() if reason}
        self.cascade(graph, blocked)
        layers = self.layer(graph, blocked)
        pin_notes = self.cabal_preflight(keep)
        for name, text in pin_notes.items():
            notes[name] = (notes[name] + ", " + text) if name in notes else text
        return Plan(layers, blocked, graph, notes, done, unresolved, dict(self.warnings), dict(self.bootstrap), dict(self.f44_bootstrap))

    def cabal_preflight(self, keep):
        store = self.specs
        checks = []
        for name in sorted(keep):
            spec_name = show(name)
            if not spec_name.startswith("ghc-") or spec_name.startswith("ghc9"):
                continue
            spec = store.get(spec_name)
            evr = spec.evr if spec is not None else None
            if not evr or len(evr) < 2 or not evr[1]:
                evr = store.evr(spec_name)[0]
                if not evr or len(evr) < 2 or not evr[1]:
                    continue
            try:
                text = Path(spec.path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "ghc-rpm-macros" not in text and "haskell_setup" not in text:
                continue
            m = re.search(r"(?m)^%global pkg_name\s+(\S+)", text)
            checks.append((name, spec_name, m.group(1) if m else spec_name[4:], str(evr[1])))
        if not checks:
            return {}
        self.progress("Checking cabal version pins")
        cache_dir = self.tool.cache_dir
        with ThreadPoolExecutor(max_workers=max(self.jobs * 4, 32)) as pool:
            fetched = list(pool.map(lambda c: _cabal.cached_deps(c[2], c[3], cache_dir), checks))
        notes = {}
        planned = {show(name) for name in keep}
        for (name, spec_name, pkg_name, version), deps in zip(checks, fetched):
            if not isinstance(deps, dict):
                continue
            for dep, constraint in sorted(deps.items()):
                if not constraint:
                    continue
                dep_spec = "ghc-" + dep
                is_planned = dep_spec in planned
                if not is_planned and dep_spec not in store:
                    continue
                dep_spec_obj = store.get(dep_spec)
                dep_evr = dep_spec_obj.evr if dep_spec_obj is not None else None
                if not dep_evr or len(dep_evr) < 2 or not dep_evr[1]:
                    dep_evr = store.evr(dep_spec)[0]
                if not dep_evr or len(dep_evr) < 2 or not dep_evr[1]:
                    continue
                if is_planned:
                    got = str(dep_evr[1])
                    source = "the planned build of " + dep_spec
                else:
                    got = None
                    for repo in self.repos.values():
                        for flag, evr, _bin in repo.provides.get(dep_spec + "-devel", ()):
                            if flag == "=":
                                got = evr[1] if isinstance(evr, tuple) and len(evr) > 1 else str(evr).rsplit("-", 1)[0]
                                break
                        if got:
                            break
                    if not got:
                        continue
                    source = dep_spec + " as published"
                want = _cabal._tuple_of(got)
                if want is None or _cabal.constraint_ok(constraint, want):
                    continue
                if not is_planned and dep_spec not in self.force:
                    self.force.add(dep_spec)
                    self.done.discard(dep_spec)
                    self.fresh_notes[dep_spec] = "cabal version pin from another package needs a rebuild, adding it to the chain"
                    self._pin_forced.append(dep_spec)
                    continue
                text = ("cabal of %s-%s pins %s %s but the build gets %s from %s"
                        % (pkg_name, version, dep, constraint, got, source))
                notes[name] = (notes[name] + ", " + text) if name in notes else text
        return notes

    def split_cycles(self, graph, reasons, notes):
        while True:
            components = [part for part in tarjan(graph) if len(part) > 1]
            if not components:
                return
            component = components[0]
            members = set(component)
            candidates = [name for name in component if alt_args(self.specs[name])]
            inside = {name: len(graph[name] & members) for name in component}
            candidates.sort(key=lambda name: (-inside[name], name))
            result = None
            for name in candidates:
                result = self.try_split(graph, members, [name])
                if result:
                    break
            if not result:
                for count in range(2, len(candidates) + 1):
                    result = self.try_split(graph, members, candidates[:count])
                    if result:
                        break
            if not result:
                text = "dependency cycle with no working bootstrap option: " + ", ".join(component)
                stuck = []
                for arch in self.arches:
                    part = {name: {dep for dep in graph[name] & members if arch in self.edge_arches.get((name, dep), ())}
                            for name in component}
                    if any(len(piece) > 1 for piece in tarjan(part)):
                        stuck.append(arch)
                if stuck and len(stuck) < len(self.arches):
                    cut = False
                    for name in component:
                        for dep in sorted(graph[name] & members):
                            if set(self.edge_arches.get((name, dep), ())) <= set(stuck):
                                graph[name].discard(dep)
                                cut = True
                    if cut and not any(len(piece) > 1 for piece in tarjan({name: graph[name] & members for name in component})):
                        for name in component:
                            notes[name] = "on " + ", ".join(stuck) + " first build on fedora-44 mock, publish, then rebuild on or11"
                            self.f44_bootstrap.setdefault(name, []).extend(stuck)
                        continue
                    text += " (the cycle only exists on " + ", ".join(stuck) + ", the repo has no packages there to start from, bootstrap it there first)"
                for name in component:
                    reasons[name].append(text)
                    graph[name] = {dep for dep in graph[name] if dep not in members}
                continue
            trial, applied = result
            graph.clear()
            graph.update(trial)
            for name in applied:
                self.bootstrap[name] = alt_args(self.specs[name])
                if not has_switch(self.specs[name]):
                    reasons[name].append("spec has no %oreon_bootstrap line, run plan bootstrap --write")
                else:
                    notes[name] = "listed twice, the first build bootstraps itself while its BuildRequires do not resolve"

    def alt_owners(self, name):
        if name not in self.alt_cache:
            spec = self.specs[name]
            try:
                owners, _missing = self.resolve_spec(spec, self.own_requirements(spec, extra=alt_args(spec)))
            except PlanError:
                owners = None
            self.alt_cache[name] = owners
        return self.alt_cache[name]

    def try_split(self, graph, members, option):
        trial = {key: set(value) for key, value in graph.items()}
        boots = set()
        for name in option:
            owners = self.alt_owners(name)
            if owners is None:
                return None
            owners = set(owners)
            if any(dep not in graph and dep not in self.done for dep in owners):
                return None
            trial[name + BOOT] = {dep for dep in owners if dep in graph}
            boots.add(name + BOOT)
        chosen = set(option)
        for node in list(members) + sorted(boots):
            for name in chosen:
                if name in trial[node] and node != name:
                    trial[node].discard(name)
                    trial[node].add(name + BOOT)
        for name in option:
            trial[name].add(name + BOOT)
        part = {key: value for key, value in trial.items() if key in members or key in boots}
        if all(len(piece) == 1 for piece in tarjan(part)):
            return trial, list(option)
        return None

    def cascade(self, graph, blocked):
        reverse = defaultdict(set)
        for name, deps in graph.items():
            for dep in deps:
                reverse[dep].add(name)
        queue = deque(blocked)
        while queue:
            name = queue.popleft()
            for user in reverse.get(name, ()):
                if user not in blocked:
                    blocked[user] = ["blocked prerequisite: " + show(name)]
                    queue.append(user)
        for key in list(blocked):
            if key.endswith(BOOT):
                base = key.removesuffix(BOOT)
                blocked.setdefault(base, blocked[key])

    def layer(self, graph, blocked):
        live = {name for name in graph if name not in blocked and show(name) not in blocked}
        remaining = set(live)
        layers = []
        while remaining:
            ready = sorted(name for name in remaining
                           if all(dep not in remaining for dep in graph[name]))
            if not ready:
                raise PlanError("internal error, cycle left in " + ", ".join(sorted(remaining)[:10]))
            layers.append(ready)
            remaining -= set(ready)
        return layers


DEFAULT_GROUPS = [
    ("rust-crates", ["rust-*"]),
    ("haskell", ["ghc-*", "ghc", "cabal-*", "alex", "happy", "pandoc*", "shellcheck"]),
    ("ocaml", ["ocaml-*", "ocaml", "dune*"]),
    ("perl", ["perl-*", "perl"]),
    ("ruby", ["rubygem-*", "ruby", "ruby-*"]),
    ("python", ["python-*", "python3*", "py*", "pytest*", "python"]),
    ("golang", ["golang-*", "golang", "go-*"]),
    ("java", ["maven*", "jackson*", "plexus*", "apache-commons-*", "jakarta-*", "javapackages*", "xmvn*",
              "java-*", "jetty*", "tomcat*", "ant", "ant-*", "junit*", "slf4j*", "guava", "guice*"]),
    ("texlive", ["texlive*", "tex-*"]),
    ("mingw", ["mingw-*"]),
    ("kde", ["kf5-*", "kf6-*", "plasma-*", "qt5-*", "qt6-*", "k3b", "kdevelop*", "kde-*", "ktorrent", "libkscreen*",
             "kio-*", "kmail*", "kontact*", "akonadi*", "kdepim*", "kate", "konsole", "dolphin*", "okular*"]),
    ("gnome", ["gnome-*", "gtk*", "glib*", "gdk-*", "libadwaita", "mutter", "gjs", "nautilus*", "evolution*", "gvfs*",
               "gstreamer*", "pango*", "cairo*", "atk*", "at-spi2-*", "gdm", "libsoup*", "webkitgtk*", "epiphany",
               "totem*", "gedit*", "libgnome*", "libgedit*"]),
    ("fonts", ["*-fonts", "*-fonts-*", "google-noto-*"]),
    ("core", ["glibc", "gcc", "binutils", "gdb", "llvm*", "clang*", "lld*", "kernel*", "systemd*", "rpm", "dnf*",
              "libdnf*", "bash", "coreutils", "util-linux", "openssl*", "filesystem", "setup", "glibc-*", "grub2*",
              "dracut", "make", "cmake", "meson", "ninja-build", "autoconf", "automake", "libtool", "pkgconf*"]),
]

_GROUP_HINTS = (
    ("rust-crates", re.compile(r"^(cargo-rpm-macros|rust-packaging)\b")),
    ("python", re.compile(r"^(python3-devel|pyproject-rpm-macros)\b")),
    ("haskell", re.compile(r"^ghc-rpm-macros\b")),
    ("java", re.compile(r"^(maven-local|javapackages-local|javapackages-bootstrap)\b")),
    ("ocaml", re.compile(r"^ocaml\b")),
    ("golang", re.compile(r"^(go-rpm-macros|golang)\b")),
    ("kde", re.compile(r"^(extra-cmake-modules|kf6-rpm-macros|cmake\(KF[56])")),
)


class Groups:
    def __init__(self, root):
        self.root = Path(root)
        self.rules = [(name, pattern) for name, patterns in DEFAULT_GROUPS for pattern in patterns]

    def of(self, spec):
        marker = self.root / spec.name / "chain.group"
        if marker.is_file():
            text = marker.read_text(encoding="utf-8").strip().split()
            if text:
                return text[0]
        for name, pattern in self.rules:
            if fnmatch.fnmatchcase(spec.name, pattern):
                return name
        for requirement in spec.brs.get("x86_64", ()):
            for name, rule in _GROUP_HINTS:
                if rule.match(requirement):
                    return name
        return "misc"

    def mapping(self, specs):
        return {name: self.of(spec) for name, spec in specs.items()}


def split_by_group(plan, group_of, wanted, with_prereqs=False):
    """Layers for one group. Needs from other groups are listed, or included."""
    names = {name for layer in plan.layers for name in layer}
    graph = plan.graph
    take = {name for name in names if group_of.get(show(name)) in wanted}
    for name in list(take):
        if name.endswith(BOOT):
            if show(name) in names:
                take.add(show(name))
        elif name + BOOT in names:
            take.add(name + BOOT)
    needs = defaultdict(set)
    for name in sorted(take):
        for dep in graph.get(name, ()):
            if dep in names and dep not in take:
                needs[group_of.get(show(dep), "misc")].add(show(dep))
    if with_prereqs:
        queue = deque(take)
        while queue:
            name = queue.popleft()
            extra = [show(name)] if name.endswith(BOOT) and show(name) in names else []
            if name + BOOT in names:
                extra.append(name + BOOT)
            for item in extra:
                if item not in take:
                    take.add(item)
                    queue.append(item)
            for dep in graph.get(name, ()):
                if dep in names and dep not in take:
                    take.add(dep)
                    queue.append(dep)
        needs = defaultdict(set)
    layers = [[name for name in layer if name in take] for layer in plan.layers]
    return [layer for layer in layers if layer], {key: sorted(value) for key, value in needs.items()}
