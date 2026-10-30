# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Parse cabal build-depends and evaluate version constraints, disk cached."""

import io
import pickle
import re
import tarfile
import urllib.request
from pathlib import Path


def _tuple_of(text):
    parts = text.strip().strip(".").split(".")
    out = []
    for part in parts:
        if not part.isdigit():
            return None
        out.append(int(part))
    return tuple(out)


def _cmp(a, b):
    n = max(len(a), len(b))
    a = tuple(a) + (0,) * (n - len(a))
    b = tuple(b) + (0,) * (n - len(b))
    return (a > b) - (a < b)


def constraint_ok(constraint, version):
    if not constraint or constraint.strip() in ("-any", "*"):
        return True
    version = tuple(version)
    for clause in constraint.split("&&"):
        clause = clause.strip()
        wildcard = clause.endswith(".*")
        if wildcard:
            clause = clause[: -len(".*")]
        m = re.match(r"^(>=|>|<=|<|==|\^>=)\s*([0-9][0-9.]*)$", clause)
        if not m:
            return True
        op, bound = m.group(1), _tuple_of(m.group(2))
        if bound is None:
            return True
        if wildcard:
            vpref = version[: len(bound)]
            if len(version) < len(bound):
                vpref = version + (0,) * (len(bound) - len(version))
            c = _cmp(vpref, bound)
            if op == "==" and c != 0:
                return False
            continue
        c = _cmp(version, bound)
        if op == ">=" and c < 0:
            return False
        if op == ">" and c <= 0:
            return False
        if op == "<=" and c > 0:
            return False
        if op == "<" and c >= 0:
            return False
        if op == "==" and c != 0:
            return False
        if op == "^>=" and (c < 0 or version[0] != bound[0]):
            return False
    return True


def _add_dep(deps, part):
    part = part.strip()
    if not part:
        return
    m = re.match(r"^([A-Za-z][A-Za-z0-9-]*)\s*(.*)$", part)
    if not m:
        return
    name, constraint = m.group(1), m.group(2).strip()
    if name in ("else", "if"):
        return
    old = deps.get(name)
    deps[name] = constraint if not old else old + " && " + constraint


_SECTIONS = ("library", "executable", "foreign-library")


def parse_deps(cabal_text):
    deps = {}
    section = None
    lines = cabal_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        head = re.match(r"^(library|executable|foreign-library|test-suite|benchmark|flag|common|source-repository|custom-setup|static-library)\b", line.strip())
        if line.strip() and not line[0].isspace() and head:
            section = head.group(1)
            i += 1
            continue
        m = re.match(r"^(\s*)build-depends\s*:\s*(.*)$", line)
        if not m or section not in _SECTIONS:
            i += 1
            continue
        indent = len(m.group(1))
        buf = [m.group(2)]
        i += 1
        while i < len(lines):
            line = lines[i]
            if not line.strip() or line.strip().startswith("--"):
                i += 1
                continue
            if len(line) - len(line.lstrip()) <= indent:
                break
            buf.append(line.strip())
            i += 1
        for part in " ".join(buf).split(","):
            _add_dep(deps, part)
    return deps


def fetch_cabal_text(pkg_name, version, timeout=30.0):
    url = "https://hackage.haskell.org/package/%s-%s/%s-%s.tar.gz" % (pkg_name, version, pkg_name, version)
    data = urllib.request.urlopen(url, timeout=timeout).read()
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for member in tf.getmembers():
            if member.name.rsplit("/", 1)[-1] == pkg_name + ".cabal":
                return tf.extractfile(member).read().decode("utf-8", "replace")
    return None


def cached_deps(pkg_name, version, cache_dir):
    cache = Path(cache_dir) / "cabal"
    cache.mkdir(parents=True, exist_ok=True)
    stored = cache / ("%s-%s.pickle" % (pkg_name, version))
    if stored.exists():
        try:
            with open(stored, "rb") as handle:
                return pickle.load(handle)
        except Exception:
            pass
    try:
        text = fetch_cabal_text(pkg_name, version)
    except Exception as err:
        return "fetchfail: %s" % err
    result = {} if text is None else parse_deps(text)
    with open(stored, "wb") as handle:
        pickle.dump(result, handle, protocol=5)
    return result
