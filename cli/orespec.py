# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import click

SPECS = Path("/home/blester/rpm-specfiles/fedora-rpms/oreon-11-rp1")
CACHE = Path.home() / ".cache" / "orespec-src"

SECTION_RE = re.compile(
    r"(?mi)^(%(?:package|description|files|prep|build|install|check|changelog|pre|post|preun|postun|pretrans|posttrans|generate_buildrequires|trigger\w*|verifyscript)\b.*)$"
)


def raw_global(raw: str, name: str) -> str | None:
    m = re.search(rf"(?m)^%global\s+{re.escape(name)}\s+(.+)$", raw)
    return m.group(1).strip() if m else None


def raw_globals(raw: str, name: str | None = None, version: str | None = None) -> dict:
    out = {}
    if name is not None:
        out["name"] = name
    if version is not None:
        out["version"] = version
    for m in re.finditer(r"(?m)^%(?:global|define)\s+(\w+)\s+(.+)$", raw):
        if m.group(1) not in out:
            out[m.group(1)] = m.group(2).strip()
    for _ in range(8):
        changed = False
        for k, v in out.items():
            nv = v
            for g, gv in out.items():
                if g != k:
                    nv = nv.replace("%{" + g + "}", gv)
            if nv != v:
                out[k] = nv
                changed = True
        if not changed:
            break
    resolved = {}
    for k, v in out.items():
        if "%" not in v:
            resolved[k] = v
            continue
        m = re.fullmatch(r"%\((.*)\)", v, re.S)
        if m and "%{" not in m.group(1):
            import subprocess
            r = subprocess.run(["sh", "-c", m.group(1)], capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and "%" not in r.stdout:
                resolved[k] = r.stdout.strip()
    return resolved


def detect_lang(raw: str) -> str:
    if re.search(r"(?m)^%cargo_build\b|^%cargo_install\b|%cargo_vendor", raw):
        return "cargo"
    if re.search(r"(?m)^%pyproject_wheel\b|%pyproject_buildrequires|%pypi_source", raw):
        return "python"
    if re.search(r"(?m)^%cmake_build\b|^%cmake_install\b|%\{cmake\}", raw):
        return "cmake"
    if re.search(r"(?m)^%gopkg\b|^%gobuild\b|go build ", raw):
        return "golang"
    if re.search(r"(?m)^%nodejs_|npm ci|npm run", raw):
        return "node"
    if re.search(r"(?m)^%make_build|^%make_install", raw):
        return "make"
    return "none"


def flatten_conds(raw: str) -> str:
    flags = {m.group(1): m.group(2) for m in re.finditer(r"(?m)^%global\s+(\w+)\s+([01])\s*$", raw)}
    out = []

    def false_cond(c: str) -> bool:
        cl = c.lower()
        if ("rhel" in cl or "fedora" in cl) and "oreon" not in cl:
            return True
        probe = c
        for k, v in flags.items():
            probe = probe.replace("%{" + k + "}", v)
        m = re.fullmatch(r"\s*([01])\s*", probe)
        return m is not None and m.group(1) == "0"

    def true_cond(c: str) -> bool:
        probe = c
        for k, v in flags.items():
            probe = probe.replace("%{" + k + "}", v)
        m = re.fullmatch(r"\s*([01])\s*", probe)
        return m is not None and m.group(1) == "1"

    stack = []
    for line in raw.splitlines():
        s = line.strip()
        mif = re.match(r"^%(?:if|ifarch|ifnarch)\b(.*)$", s)
        if mif:
            if stack and stack[-1] in ("take_else", "skip"):
                stack.append("skip")
            elif s.startswith("%ifarch") or s.startswith("%ifnarch"):
                stack.append("keep")
                out.append(line)
            elif false_cond(mif.group(1)):
                stack.append("take_else")
            elif true_cond(mif.group(1)):
                stack.append("take_if")
            else:
                stack.append("keep")
                out.append(line)
            continue
        if re.match(r"^%else\b", s) and stack:
            top = stack[-1]
            if top == "skip":
                continue
            if top == "take_else":
                stack[-1] = "take_if"
                continue
            if top == "take_if":
                stack[-1] = "take_else"
                continue
            out.append(line)
            continue
        if re.match(r"^%endif\b", s) and stack:
            if stack.pop() == "keep":
                out.append(line)
            continue
        if stack and stack[-1] in ("take_else", "skip"):
            continue
        out.append(line)
    if stack:
        return raw if raw.endswith("\n") else raw + "\n"
    return "\n".join(out) + "\n"


def resolve_source0(spec_path: Path, raw: str, name: str, version: str) -> str | None:
    m = re.search(r"(?m)^Source0?:\s+([^\n]+)", raw)
    if not m:
        return None
    line = m.group(1).strip()
    probe = line
    if probe.startswith("%{") and probe.endswith("}"):
        probe = probe[2:-1]
    probe = probe.replace("%{version}", version).replace("%{name}", name)
    if probe.startswith("pypi_source"):
        parts = probe.split()
        pkg = parts[1].strip("{}") if len(parts) >= 2 else name
        ver = parts[2].strip("{}") if len(parts) >= 3 else version
        return f"https://files.pythonhosted.org/packages/source/{pkg[0]}/{pkg}/{pkg}-%{{version}}.tar.gz"
    if probe == "crates_source":
        crate = raw_global(raw, "crate") or name
        return f"https://static.crates.io/crates/{crate}/{crate}-%{{version}}.crate"
    if "%" in line:
        stripped = re.sub(r"%\{(version|name|release|epoch)\}", "", line)
        if "%" not in stripped:
            return line
        return expand_source0(spec_path, raw, name, version)
    return line


def expand_source0(spec_path: Path, raw: str, name: str, version: str) -> str | None:
    import subprocess

    try:
        expanded = subprocess.run(["rpmspec", "-P", str(spec_path)], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return None
    m = re.search(r"(?mi)^Source0?:\s+(\S+)\s*$", expanded)
    if not m or "%" in m.group(1):
        return None
    url = m.group(1)
    pairs = list(raw_globals(raw, name, version).items())
    for g, v in sorted(pairs, key=lambda p: -len(p[1])):
        if v and v in url:
            url = url.replace(v, "%{" + g + "}")
    known = "|".join(g for g, _ in pairs)
    if "%" in re.sub(r"%\{(?:" + known + r")\}", "", url):
        return None
    return url


def spec_sections(raw: str) -> list[tuple[str, str]]:
    out = []
    matches = list(SECTION_RE.finditer(raw))
    pre = raw[: matches[0].start()] if matches else raw
    out.append(("", pre))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        out.append((m.group(1), raw[m.end() : end]))
    return out


def spec_facts(spec_path: Path, raw: str | None = None) -> dict:
    import rpm

    if raw is None:
        raw = spec_path.read_text()
        spec = rpm.spec(str(spec_path))
    else:
        tmp = spec_path.with_name(spec_path.name + ".orespec-tmp")
        tmp.write_text(raw)
        try:
            spec = rpm.spec(str(tmp))
        finally:
            tmp.unlink(missing_ok=True)
    h = spec.sourceHeader
    facts = {}
    facts["name"] = h[rpm.RPMTAG_NAME]
    facts["version"] = h[rpm.RPMTAG_VERSION]
    rel = (h[rpm.RPMTAG_RELEASE] or "1").strip()
    rel = re.sub(r"%\{?\??dist\}?$", "", rel)
    rel = re.sub(r"\.fc\d+$", "", rel)
    if "%" in rel or not rel.isdigit():
        rel = "1"
    facts["release"] = rel
    facts["epoch"] = h[rpm.RPMTAG_EPOCHNUM]
    facts["summary"] = h[rpm.RPMTAG_SUMMARY]
    facts["license"] = h[rpm.RPMTAG_LICENSE]
    facts["url"] = h[rpm.RPMTAG_URL] or None
    facts["bdeps"] = sorted({d for d in (h[rpm.RPMTAG_REQUIRES] or []) if not d.startswith("rpmlib(")})
    facts["source0_hash"] = raw_global(raw, "source0_hash")
    facts["source0_url"] = resolve_source0(spec_path, raw, facts["name"], facts["version"])
    facts["lang"] = detect_lang(raw)
    facts["noarch"] = h[rpm.RPMTAG_ARCH] == "noarch"
    d_global = raw_global(raw, "_description")
    m = re.search(r"(?ms)^%global\s+_description\s+%\{expand:\s*(.*?)\}\s*$", raw)
    if m:
        facts["description"] = m.group(1).strip()
    elif d_global and "%" not in d_global:
        facts["description"] = d_global.strip()
    else:
        m2 = re.search(r"(?mi)^%description\b[^\n]*\n(.*?)(?=^\%(?:package|description|files|prep|build|install|check|changelog)\b)", raw, re.S)
        txt = m2.group(1).strip() if m2 else ""
        facts["description"] = txt if txt and "%" not in txt else None
    extras = []
    payload = f"python3-{facts['name'].removeprefix('python-')}" if facts["lang"] == "python" else None
    facts["payload"] = payload
    if payload:
        m = re.search(r"(?mi)^%pyproject_extras_subpkg\s+(.+)$", raw)
        if m:
            last = m.group(1).split()[-1]
            extras = [e.strip() for e in last.split(",") if e.strip()]
    facts["extras"] = extras
    subs = []
    extras_subnames = {f"{payload}+{e}" for e in extras}
    for pkg in spec.packages:
        ph = getattr(pkg, "header", pkg)
        name = ph[rpm.RPMTAG_NAME]
        if name == facts["name"] or name in extras_subnames or "+" in name:
            continue
        subs.append({"name": name, "summary": ph[rpm.RPMTAG_SUMMARY]})
    facts["subpackages"] = subs
    return facts


URL_MIGRATIONS = [
    (re.compile(r"https?://(?:www\.)?apache\.org/dist/"), "https://archive.apache.org/dist/"),
    (re.compile(r"https?://ftp\.alsa-project\.org/pub/"), "https://www.alsa-project.org/files/pub/"),
    (re.compile(r"https?://sourceforge\.net/projects/(\S+?)/files(\S*?)/download$"), r"https://downloads.sourceforge.net/project/\1\2"),
]


def file_sha256(fn: Path) -> str:
    h = hashlib.sha256()
    with open(fn, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sniff_html(fn: Path) -> bool:
    with open(fn, "rb") as f:
        head = f.read(256).lstrip()
    return head[:1] == b"<" or head[:16].lower().startswith(b"<!doctype")


UAS = ("orespec-fetch/1.0", "Mozilla/5.0 (X11; Linux x86_64) orespec-fetch", "curl/8.9.1")

LAST_FINAL_URL = ""


def _download(real: str, fn: Path) -> str:
    global LAST_FINAL_URL
    import shutil
    import urllib.error
    import urllib.request

    tmp = fn.with_name(fn.name + ".part")
    last = None
    for ua in UAS:
        try:
            req = urllib.request.Request(real, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=25) as r, open(tmp, "wb") as o:
                shutil.copyfileobj(r, o)
            if _sniff_html(tmp):
                tmp.unlink(missing_ok=True)
                raise RuntimeError("HTML body")
            tmp.replace(fn)
            LAST_FINAL_URL = real
            return real
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 403, 404, 410):
                for pat, repl in URL_MIGRATIONS:
                    if pat.search(real):
                        mig = pat.sub(repl, real)
                        nfn = CACHE / (hashlib.sha256(mig.encode()).hexdigest()[:16] + "-" + Path(mig).name)
                        if not nfn.exists():
                            _download(mig, nfn)
                        fn.unlink(missing_ok=True)
                        nfn.replace(fn)
                        LAST_FINAL_URL = mig
                        return mig
            raise
        except RuntimeError:
            last = None
    if last is None:
        raise RuntimeError(f"HTML body for {real}")
    raise last


def fetch_source(url: str, name: str, version: str, want_hash: str | None) -> Path:
    global LAST_FINAL_URL
    real = url.replace("%{version}", version).replace("%{name}", name)
    CACHE.mkdir(parents=True, exist_ok=True)
    fn = CACHE / (hashlib.sha256(real.encode()).hexdigest()[:16] + "-" + Path(real).name)
    LAST_FINAL_URL = real
    if not fn.exists():
        LAST_FINAL_URL = _download(real, fn)
    elif _sniff_html(fn):
        fn.unlink()
        LAST_FINAL_URL = _download(real, fn)
    h = file_sha256(fn)
    if want_hash and want_hash != "none" and h != want_hash:
        raise SystemExit(f"{name}: hash mismatch {h} != {want_hash}")
    return fn


def pep_dep_caps(req: str) -> list[str]:
    req = req.split(";")[0].strip()
    m = re.match(r"^([A-Za-z0-9_.-]+)\s*(\[[^\]]*\])?\s*(.*)$", req)
    if not m:
        return []
    base, extras, rest = m.groups()
    n = re.sub(r"[-_]+", "-", base.lower())
    if extras:
        n += re.sub(r"\s+", "", extras.lower())
    caps = []
    for part in rest.split(","):
        part = part.strip()
        if not part:
            continue
        cm = re.match(r"^([<>=!~]+)\s*(.+)$", part)
        if cm:
            ops, ver = cm.groups()
            op = ops.replace("~", ">").strip()
            if op not in ("<", "<=", ">", ">=", "=="):
                caps.append(f"python3dist({n})")
            else:
                caps.append(f"python3dist({n}) {op} {ver}")
        else:
            caps.append(f"python3dist({n}) {part}")
    if any(" " in c for c in caps):
        caps = [c for c in caps if " " in c]
    return caps


def dedupe_caps(caps: list[str]) -> list[str]:
    seen = set()
    out = []
    for c in caps:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def pyproject_manifest(facts: dict) -> dict:
    url = facts["source0_url"]
    if not url:
        raise SystemExit(f"{facts['name']}: cannot resolve source url for pyproject.toml")
    fn = fetch_source(url, facts["name"], facts["version"], None)
    data = {}
    if fn.name.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
        with tarfile.open(fn) as tf:
            for m in tf.getmembers():
                if m.name.endswith("/pyproject.toml") or m.name == "pyproject.toml":
                    data = tomllib.load(tf.extractfile(m))
                    break
    elif fn.name.endswith((".zip", ".whl")):
        with zipfile.ZipFile(fn) as zf:
            cands = [n for n in zf.namelist() if n.endswith("/pyproject.toml") or n == "pyproject.toml"]
            if cands:
                data = tomllib.load(io.BytesIO(zf.read(cands[0])))
    proj = data.get("project", {})
    out = {
        "build": [],
        "run": [],
        "extras": {},
    }
    for r in data.get("build-system", {}).get("requires", []):
        out["build"].extend(pep_dep_caps(r))
    for r in proj.get("dependencies", []):
        out["run"].extend(pep_dep_caps(r))
    for extra, deps in (proj.get("optional-dependencies") or {}).items():
        caps = []
        for r in deps:
            caps.extend(pep_dep_caps(r))
        out["extras"][extra] = caps
    return out


def py_globs(payload_mod: str) -> list[str]:
    return [
        f"usr/lib/python3*/site-packages/{payload_mod}*",
        f"usr/lib64/python3*/site-packages/{payload_mod}*",
        "usr/bin/*",
    ]


def sub_globs_for(sname: str, lang: str, payload: str | None) -> list[str]:
    if lang == "python" and sname == payload:
        return py_globs(payload.removeprefix("python3-").replace("-", "_"))
    if sname.endswith("-devel"):
        return [
            "usr/include/*",
            "usr/*/pkgconfig/*",
            "usr/lib*/cmake/*",
            "usr/share/aclocal/*",
            "usr/*/lib*.so",
            "usr/share/cmake/*",
        ]
    if sname.startswith("lib"):
        return ["usr/*/lib*.so.*"]
    return ["usr/bin/*", "usr/sbin/*", "usr/share/man/*", "usr/share/applications/*"]


def payload_of(facts: dict) -> str | None:
    return facts.get("payload")


def src_num_ok(l: str, declared: set) -> bool:
    return all(m.group(1) in declared for m in re.finditer(r"%(?:SOURCE|source)(\d+)\b", l, re.I))


def strip_undropped_a(l: str, declared: set) -> str:
    def repl(m):
        nums = m.group(1).split(",")
        return "" if all(n not in declared for n in nums) else m.group(0)
    return re.sub(r"(?i)-a\s*(\d+(?:,\d+)*)\b", repl, l)


def repair_parse_text(raw: str) -> str:
    raw = re.sub(
        r"(?mi)^%description[^\n]*$",
        lambda m: re.sub(r"\s+%(?:\{)?_description(?:\})?.*$", "", m.group(0)),
        raw,
    )

    def fix_bad_dep(m):
        pre, name, extra, ver, _comma = m.groups()[:5] if len(m.groups()) >= 5 else (m.group(1), m.group(2), m.group(3), m.group(4), None)
        n = re.sub(r"[-_]+", "-", name.lower())
        ver = re.sub(r"^([<>=!~]+)\s*", r"\1 ", ver).strip() if ver else ""
        ver = f" {ver}" if ver else ""
        return f"{pre}python3dist({n}[{extra.lower()}]){ver}"

    raw = re.sub(
        r"(?im)^((?:Build)?Requires:\s+)python3dist\(([A-Za-z0-9._-]+)\)\s*\[([^\]]+)\]\s*([<>=!~]+\s*[^\s,]*)?\s*(,)?\s*$",
        fix_bad_dep,
        raw,
    )
    raw = re.sub(r"(?m)^((?:Build)?Requires:\s+\S+\s+)([<>=!~]+)(?=[0-9vV])", r"\1\2 ", raw)
    pkg_names = set(re.findall(r"(?im)^%package\s+-n\s+(\S+)", raw))
    m_name = re.search(r"(?im)^Name:\s+(\S+)", raw)
    if m_name:
        pkg_names.add(m_name.group(1))
    seen = set()
    kept = []
    open_ct = 0
    cond_stack = []
    cond_pkg = {}
    src_nums = {m.group(1) or "0" for m in re.finditer(r"(?im)^Source(\d*):\s+", raw)}
    lines_in = raw.splitlines()
    i = 0
    while i < len(lines_in):
        l = lines_in[i]
        s = l.strip()
        m_f = re.match(r"(?i)^%files\s+-n\s+(\S+)", s)
        if m_f and m_f.group(1) in cond_pkg:
            j = i + 1
            while j < len(lines_in) and not re.match(r"^%(?:package|description|files|prep|build|install|check|changelog|generate_buildrequires)\b", lines_in[j].strip()):
                j += 1
            for c in cond_pkg[m_f.group(1)]:
                kept.append(c)
            kept.extend(lines_in[i:j])
            for _c in cond_pkg[m_f.group(1)]:
                kept.append("%endif")
            i = j
            continue
        if m_f and m_f.group(1) not in pkg_names:
            i += 1
            continue
        m_af = re.match(r"^%oreon_autofiles\s+(\S+)", s)
        if m_af and m_af.group(1) != "main" and m_af.group(1) not in pkg_names:
            i += 1
            continue
        m_src = re.match(r"(?i)^(?:Source\d*):\s+(\S+)\s*$", s)
        if not (s.startswith("#") or l.startswith("#")) and m_src:
            if m_src.group(1) in seen:
                i += 1
                continue
            seen.add(m_src.group(1))
        if re.match(r"^(cat|echo)\b", s) and not src_num_ok(s, src_nums):
            i += 1
            continue
        if re.match(r"^if\s+.*\.cargo/config", s):
            depth = 1
            j = i + 1
            block = []
            while j < len(lines_in) and depth > 0:
                s2 = lines_in[j].strip()
                if re.match(r"^if\b", s2):
                    depth += 1
                elif re.match(r"^fi\b", s2):
                    depth -= 1
                if depth > 0:
                    block.append(s2)
                j += 1
            body_ok = all(re.match(r"^(cat|echo|else|elif)\b", b) or not b for b in block)
            refs_ok = all(src_num_ok(b, src_nums) for b in block if re.match(r"^(cat|echo)\b", b))
            if body_ok and not refs_ok:
                i = j
                continue
        if re.match(r"^%autosetup\b", s):
            l = strip_undropped_a(l, src_nums)
        kept.append(l)
        i += 1
        if s.startswith("#"):
            continue
        if re.match(r"^%(?:if|ifarch|ifnarch)\b", s):
            cond_stack.append(s)
            open_ct += 1
        elif re.match(r"^%endif\b", s):
            if open_ct == 0:
                kept.pop()
                continue
            open_ct -= 1
            if cond_stack:
                cond_stack.pop()
        m_p = re.match(r"(?i)^%package\s+-n\s+(\S+)", s)
        if m_p and cond_stack:
            cond_pkg[m_p.group(1)] = list(cond_stack)
    kept.extend(["%endif"] * open_ct)
    return "\n".join(kept) + ("\n" if raw.endswith("\n") else "")


def rpm_header(spec_path: Path, text: str):
    import rpm as _rpm

    tmp = spec_path.with_name(spec_path.name + ".orespec-tmp")
    tmp.write_text(text)
    try:
        return _rpm.spec(str(tmp)).sourceHeader
    finally:
        tmp.unlink(missing_ok=True)


def retrofit_spec(spec_path: Path, fast: bool = False) -> tuple[str, list[str]]:
    raw = spec_path.read_text()
    if spec_path.name == "oreon-rpm-config.spec":
        return raw, ["ships the oreon macros, not retrofitted"]
    prep_m = re.search(r"(?ms)^%prep\n(.*?)(?=^%(?:package|description|files|build|install|check|changelog|generate_buildrequires)\b)", raw)
    src_nums = {m.group(1) or "0" for m in re.finditer(r"(?im)^Source(\d*):\s+", raw)}
    prep_txt = prep_m.group(1) if prep_m else ""
    bad_refs = any(m.group(1) not in src_nums for m in re.finditer(r"%(?:SOURCE|source)(\d+)\b", prep_txt))
    bad_refs = bad_refs or any(m.group(1) not in src_nums for m in re.finditer(r"(?i)-a(\d+)\b", prep_txt))
    bad_deps = bool(re.search(r"(?im)^(?:Build)?Requires:\s+\S+\s+\[", raw))
    if "# oreon:deps" in raw and "%oreon_autofiles" in raw and not bad_refs and not bad_deps:
        repaired = repair_parse_text(raw)
        if repaired != raw:
            raw = repaired
        import rpm as _rpm

        try:
            h = rpm_header(spec_path, raw)
        except Exception:
            raw = repair_parse_text(raw)
            h = rpm_header(spec_path, raw)
        result, hnotes = fill_hashes(raw, h[_rpm.RPMTAG_NAME], h[_rpm.RPMTAG_VERSION], fast=fast)
        return result, hnotes + ["already retrofitted, hash refresh only"]
    raw = flatten_conds(raw)
    raw = re.sub(r"(?mi)^[ \t]*%pyproject_extras_subpkg.*\n?", "", raw)
    raw = re.sub(r"(?mi)^[ \t]*%pyproject_save_files.*\n?", "", raw)
    had_gopkg = bool(re.search(r"(?mi)^[ \t]*%gopkg\b", raw))
    raw = re.sub(r"(?mi)^[ \t]*%gopkg(?:files)?\s*$\n?", "", raw)
    repaired = repair_parse_text(raw)
    if repaired != raw:
        raw = repaired
    try:
        rpm_header(spec_path, raw)
    except Exception:
        raw = repair_parse_text(raw)
        rpm_header(spec_path, raw)
    facts = spec_facts(spec_path, raw)
    notes = []
    lang = facts["lang"]
    payload = payload_of(facts)
    subs = facts["subpackages"]
    subnames = {s["name"] for s in subs}
    emitted_subs = {facts["name"]}
    glob_map = {s["name"]: sub_globs_for(s["name"], lang, payload) for s in subs}
    excludes = [g for gs in glob_map.values() for g in gs]

    extra_brs = []
    extra_secs = []
    payload_reqs = []
    if lang == "python":
        man = pyproject_manifest(facts)
        extra_brs = sorted(set(man["build"]) | {"python3-devel", "pyproject-rpm-macros"})
        payload_reqs = sorted(set(man["run"]))
        for ex, caps in man["extras"].items():
            ename = f"{payload}+{ex}"
            lines = [
                f"%package -n       {ename}",
                f"Summary:        Metapackage for {payload} {ex} extras",
                f"Requires:       {payload} = %{{version}}-%{{release}}",
            ]
            lines.extend(f"Requires:       {c}" for c in dedupe_caps(caps))
            lines.append(f"Provides:       python3dist({facts['name'].removeprefix('python-')}[{ex}]) = %{{version}}-%{{release}}")
            lines.append("")
            lines.append(f"%description -n {ename}")
            lines.append(f"Metapackage for {payload} {ex} extras.")
            extra_secs.append("\n".join(lines) + "\n")
        notes.append(f"explicit run deps: {payload_reqs}")
        notes.append(f"explicit extras: {sorted(man['extras'])}")
    elif lang == "cargo":
        extra_brs = [d for d in facts["bdeps"] if not re.match(r"^(rust-|crate\(|rustpack)", d)] + ["rustc", "cargo", "cargo-rpm-macros"]
        extra_brs = sorted(set(extra_brs))
        notes.append("crate BRs dropped, vendored offline via Cargo.lock")
    elif lang == "golang":
        extra_brs = sorted({d for d in facts["bdeps"] if not d.startswith("golang(")} | {"golang"})
        if had_gopkg:
            for s in subs:
                lines = [
                    f"%package -n       {s['name']}",
                    f"Summary:        {s['summary']}",
                    "BuildArch:      noarch",
                    "",
                    f"%description -n {s['name']}",
                    f"{s['summary']}",
                ]
                extra_secs.append("\n".join(lines) + "\n")
            notes.append("golang meta BRs dropped, %gopkg expanded explicit")
        else:
            notes.append("golang meta BRs dropped, vendored offline via go.mod")
    elif lang == "node":
        extra_brs = sorted({d for d in facts["bdeps"] if not d.startswith("nodejs(")} | {"nodejs", "npm"})
        notes.append("nodejs meta BRs dropped, vendored offline via package-lock")
    else:
        extra_brs = facts["bdeps"]

    drop_sources = set()
    if lang == "cargo":
        for sn, surl in re.findall(r"(?mi)^Source(\d*):\s+(\S+)", raw):
            num = sn or "0"
            if "vendor" not in surl:
                continue
            if re.search(rf"(?i)%\{{source{num}\}}|-a\s*{num}\b", prep_txt):
                continue
            drop_sources.add(num)
    declared_srcs = {m.group(1) or "0" for m in re.finditer(r"(?im)^Source(\d*):\s+", raw)} - drop_sources
    out = []
    files_at = False

    def _bal(s: str) -> int:
        return len(re.findall(r"(?m)^[ \t]*%(?:if|ifarch|ifnarch)\b", s)) - len(re.findall(r"(?m)^[ \t]*%endif\b", s))

    for head, body in spec_sections(raw):
        if head == "":
            lines = []
            skip_desc = False
            saw_src0 = False
            for l in body.splitlines():
                if skip_desc:
                    if l.rstrip().endswith("}"):
                        skip_desc = False
                    continue
                if re.match(r"^\s*#", l) or not l.strip():
                    continue
                if re.match(r"^%global\s+_description\b", l):
                    if not l.rstrip().endswith("}"):
                        skip_desc = True
                    continue
                if re.match(r"(?i)^Release:", l):
                    lines.append(f"Release:        {facts['release']}%{{?dist}}")
                    continue
                m_src = re.match(r"(?i)^Source(\d*):\s+(\S+)", l)
                if m_src:
                    if (m_src.group(1) or "0") in drop_sources:
                        continue
                    if re.match(r"(?i)^Source0?:", l):
                        if saw_src0:
                            continue
                        if facts["source0_url"]:
                            lines.append(f"Source0:        {facts['source0_url']}")
                            saw_src0 = True
                            continue
                        raise SystemExit(f"{facts['name']}: cannot resolve Source0 url")
                m_hg = re.match(r"^%global\s+source(\d*)_hash", l)
                if m_hg and (m_hg.group(1) or "0") in drop_sources:
                    continue
                if re.match(r"(?i)^Summary:", l):
                    lines.append(f"Summary:        {facts['summary']}")
                    continue
                if re.match(r"(?i)^BuildRequires:", l):
                    continue
                if l.startswith("%pyproject_extras_subpkg"):
                    continue
                lines.append(l)
            body = "\n".join(lines).strip("\n") + "\n"
            br_lines = [f"BuildRequires:  {d}" for d in sorted(set(extra_brs))]
            req_lines = [f"Requires:       {r}" for r in sorted(set(facts.get("main_reqs", [])))]
            if payload:
                req_lines.append(f"Requires:       {payload} = %{{version}}-%{{release}}")
            tail = "# oreon:deps\n" + "\n".join(br_lines + req_lines) + "\n# /oreon:deps\n"
            out.append(body + tail + "\n".join(extra_secs) + ("\n" if extra_secs else ""))
        elif head.startswith("%prep"):
            body = re.sub(r"(?mi)^[ \t]*%cargo_prep.*\n?", "", body)
            lines = [l.strip() for l in body.strip().splitlines() if l.strip() and not l.strip().startswith("#")]
            guards = [l for l in lines if l.startswith('test "%{source')]
            lines = [l for l in lines if not l.startswith('test "%{source')]
            simple = bool(lines) and len(lines) == 1 and re.match(r"^%autosetup\b", lines[0])
            prep_n = None
            if simple:
                m_n = re.search(r"-n\s+(\S+)", lines[0])
                p_f = re.search(r"-p(\d+)", lines[0])
                if p_f and p_f.group(1) != "1":
                    simple = False
                if m_n:
                    prep_n = m_n.group(1)
                    if "%" in prep_n.replace("%{version}", facts["version"]).replace("%{name}", facts["name"]):
                        simple = False
            if simple and lang != "cargo":
                new_prep = "%prep\n"
                for g in guards:
                    new_prep += g + "\n"
                if prep_n:
                    new_prep += f"%global oreon_srcdir {prep_n}\n"
                new_prep += "%oreon_prep\n"
                out.append(new_prep)
                notes.append("prep replaced with %oreon_prep")
            elif lang == "cargo":
                guards = [g for g in guards if not (re.search(r"%\{source(\d*)_hash\}", g) and (re.search(r"%\{source(\d*)_hash\}", g).group(1) or "0") in drop_sources)]
                new_lines = []
                for l in lines:
                    m_s = re.match(r"(?i)^Source(\d*):\s+(\S+)", l)
                    if m_s and (m_s.group(1) or "0") in drop_sources:
                        continue
                    if re.match(r"^%global\s+source\d*_hash\b", l):
                        m_g = re.match(r"^%global\s+source(\d*)_hash", l)
                        if m_g and (m_g.group(1) or "0") in drop_sources:
                            continue
                    if re.match(r"^%autosetup\b", l):
                        l = re.sub(r"%\{\?(?:rhel|fedora):[^{}]*\}", "", l)
                        l = re.sub(r"\s+", " ", l).strip()
                    new_lines.append(l)
                i2 = 0
                cleaned = []
                while i2 < len(new_lines):
                    l = new_lines[i2]
                    if re.match(r"^if\s+.*\.cargo/config", l):
                        depth = 1
                        j = i2 + 1
                        block = []
                        while j < len(new_lines) and depth > 0:
                            s2 = new_lines[j].strip()
                            if re.match(r"^if\b", s2):
                                depth += 1
                            elif re.match(r"^fi\b", s2):
                                depth -= 1
                            if depth > 0:
                                block.append(s2)
                            j += 1
                        body_ok = all(re.match(r"^(cat|echo|else|elif)\b", b) or not b for b in block)
                        refs_ok = all(src_num_ok(b, declared_srcs) for b in block if re.match(r"^(cat|echo)\b", b))
                        if body_ok and not refs_ok:
                            i2 = j
                            continue
                    cleaned.append(l)
                    i2 += 1
                new_lines = cleaned
                new_lines = [l for l in new_lines if re.match(r"^%autosetup\b", l) or src_num_ok(l, declared_srcs) or not re.search(r"%(?:SOURCE|source)\d+\b", l)]
                new_lines = [l if not re.match(r"^%autosetup\b", l) else strip_undropped_a(l, declared_srcs) for l in new_lines]
                new_prep = "%prep\n" + "\n".join(guards + new_lines) + "\n%oreon_vendor_cargo\n"
                out.append(new_prep)
                notes.append("cargo prep normalized, vendored from Cargo.lock at prep")
            else:
                notes.append("kept custom %prep body")
                out.append(head + "\n" + body)
        elif head.startswith("%generate_buildrequires"):
            notes.append("dropped %generate_buildrequires")
        elif head.startswith("%install"):
            body = "\n".join(l for l in body.splitlines() if not re.match(r"^\s*#", l) and l.strip())
            body = body.strip()
            ilines = body.splitlines() if body else []
            end = len(ilines)
            while end > 0 and (re.match(r"^\s*%(if|else|endif)\b", ilines[end - 1]) or not ilines[end - 1].strip()):
                end -= 1
            af_calls = []
            for s in subs:
                if s["name"] not in emitted_subs:
                    continue
                af_calls.append(f"%oreon_autofiles {s['name']} {' '.join(glob_map[s['name']])}")
            af_calls.append(f"%oreon_autofiles main {excludes and ' '.join(excludes) or ''}".rstrip())
            body = "\n".join(ilines[:end]) + "\n" + "\n".join(af_calls) + "\n" + "\n".join(ilines[end:]) + "\n"
            out.append(head + "\n" + body)
        elif head.startswith("%files") or head.startswith("%gopkg"):
            continue
        elif head.startswith("%description"):
            m_n = re.search(r"-n\s+(\S+)", head)
            if "%_description" in head or "%{_description}" in head:
                head = re.sub(r"\s+%(?:\{)?_description(?:\})?.*$", "", head).rstrip()
                body = "\n" + (facts.get("description") or facts["summary"]) + "\n\n"
            elif body.strip() in ("%_description", "%{_description}"):
                body = "\n" + (facts.get("description") or facts["summary"]) + "\n\n"
            out.append(head + body)
        elif head.startswith("%package"):
            m_n = re.search(r"-n\s+(\S+)", head)
            sname = m_n.group(1) if m_n else None
            emitted_subs.add(sname or facts["name"])
            nb = []
            for l in body.splitlines():
                if re.match(r"^\s*#", l) or not l.strip():
                    continue
                if re.match(r"(?i)^Summary:\s*%{summary}", l.strip()) and sname:
                    lit = facts["summary"]
                    for s in subs:
                        if s["name"] == sname:
                            lit = s["summary"]
                    nb.append(f"Summary:        {lit}")
                    continue
                nb.append(l)
            if sname == payload and payload_reqs:
                nb.append("# oreon:reqs")
                nb.extend(f"Requires:       {r}" for r in payload_reqs)
                nb.append("# /oreon:reqs")
            out.append(head + "\n" + "\n".join(nb) + "\n")
        elif head.startswith("%changelog"):
            files_lines = ["%files -f %{oreon_filedir}/main.list" if not payload else "%files"]
            for s in subs:
                files_lines.append(f"%files -n {s['name']} -f %{{oreon_filedir}}/{s['name']}.list")
            out.append("\n".join(files_lines) + "\n\n")
            files_at = True
            out.append(head + "\n" + body)
        else:
            if head.startswith("%files"):
                m_n = re.search(r"-n\s+(\S+)", head)
                if m_n and m_n.group(1) not in emitted_subs:
                    continue
            if not head.startswith("%changelog"):
                body = "\n".join(l for l in body.splitlines() if not re.match(r"^\s*#", l) and l.strip())
                body = "\n" + body + "\n" if body.strip() else "\n"
            out.append(head + "\n" + body)
    if not files_at:
        files_lines = ["%files -f %{oreon_filedir}/main.list" if not payload else "%files"]
        for s in subs:
            files_lines.append(f"%files -n {s['name']} -f %{{oreon_filedir}}/{s['name']}.list")
        out.append("\n" + "\n".join(files_lines) + "\n")
    result = "".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result)
    opens = len(re.findall(r"(?m)^[ \t]*%(?:if|ifarch|ifnarch)\b", result))
    closes = len(re.findall(r"(?m)^[ \t]*%endif\b", result))
    if opens > closes:
        result = result.rstrip("\n") + "\n" + "%endif\n" * (opens - closes)
    if not result.endswith("\n"):
        result += "\n"
    result, hash_notes = fill_hashes(result, facts["name"], facts["version"], fast=fast)
    notes.extend(hash_notes)
    return result, notes


def expand_url(url: str, text: str, name: str, version: str) -> str:
    vals = raw_globals(text, name, version)
    for _ in range(5):
        new = url
        for k, v in vals.items():
            new = new.replace("%{" + k + "}", v)
        m = re.search(r"%\{pypi_source\s+([^}]+)\}", new)
        if m:
            parts = m.group(1).split()
            pkg = parts[0].strip("%{}") if parts else name
            ver = parts[1].strip("%{}") if len(parts) > 1 else version
            ext = parts[2].strip("%{}") if len(parts) > 2 else "tar.gz"
            new = new.replace(m.group(0), f"https://files.pythonhosted.org/packages/source/{pkg[0]}/{pkg}/{pkg}-{ver}.{ext}")
        if new == url:
            break
        url = new
    return url


def bioc_pin(url: str, name: str) -> str | None:
    m = re.match(r"(https://bioconductor\.org/packages/)release(/.+)", url)
    if not m:
        return None
    import urllib.error
    import urllib.request

    for rel in ("3.23", "3.22", "3.21", "3.20", "3.19", "3.18", "3.17", "3.16", "3.15", "3.14"):
        cand = m.group(1) + rel + m.group(2)
        try:
            req = urllib.request.Request(cand, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) orespec-fetch"}, method="HEAD")
            urllib.request.urlopen(req, timeout=20)
            return cand
        except urllib.error.HTTPError:
            continue
        except Exception:
            continue
    return None


def fill_hashes(text: str, name: str, version: str, fast: bool = False) -> tuple[str, list[str]]:
    hnotes = []
    for num, url in re.findall(r"(?mi)^Source(\d*):\s+(\S+)\s*$", text):
        if not re.match(r"^https?://", url):
            continue
        key = f"source{num or '0'}_hash"
        g = raw_global(text, key)
        if fast and g and re.fullmatch(r"[0-9a-f]{64}", g):
            continue
        try:
            real = expand_url(url, text, name, version)
            if "%{" in real or "%(" in real:
                raise SystemExit(f"{name}: cannot expand {url}")
            real = real.replace("%%", "%")
            try:
                fn = fetch_source(real, name, version, None)
                final = LAST_FINAL_URL
            except (Exception, SystemExit) as e:
                retry = None
                if "404" in str(e) or "403" in str(e) or "400" in str(e) or "410" in str(e) or "HTML" in str(e):
                    for pat, repl in URL_MIGRATIONS:
                        if pat.search(real):
                            retry = pat.sub(repl, real)
                            break
                    if not retry:
                        retry = bioc_pin(real, name)
                if not retry:
                    raise
                real = retry
                fn = fetch_source(real, name, version, None)
                final = LAST_FINAL_URL
                hnotes.append(f"Source{num or '0'} moved to {final}")
        except (Exception, SystemExit) as e:
            if g and re.fullmatch(r"[0-9a-f]{64}", g or ""):
                hnotes.append(f"{key} kept, fetch failed")
                continue
            raise SystemExit(f"{name}: {key} {e}") from None
        if final != real:
            vals = raw_globals(text, name, version)
            unexp = final
            for k, v in sorted(vals.items(), key=lambda p: -len(p[1])):
                if v and v in unexp:
                    unexp = unexp.replace(v, "%{" + k + "}")
            if re.search(r"%\{(?!version\}|name\}|release\})", unexp):
                unexp = final
            text = re.sub(rf"(?mi)^(Source{num or '0'}:\s+)\S+$", lambda mm: mm.group(1) + unexp, text)
            hnotes.append(f"Source{num or '0'} moved to {unexp}")
        digest = file_sha256(fn)
        if g and re.fullmatch(r"[0-9a-f]{64}", g):
            if g != digest:
                hnotes.append(f"{key} updated {g[:12]} -> {digest[:12]}")
                text = re.sub(rf"(?m)^%global\s+{re.escape(key)}\s+[0-9a-f]{{64}}\s*$", f"%global {key} {digest}", text)
        elif g == "none" or g is None:
            hnotes.append(f"{key} set to {digest[:12]}")
            if g == "none":
                text = re.sub(rf"(?m)^%global\s+{re.escape(key)}\s+none\s*$", f"%global {key} {digest}", text)
            else:
                text = f"%global {key} {digest}\n" + text
        else:
            raise SystemExit(f"{name}: bad {key} {g}")
    return text, hnotes


@click.group()
def cli():
    pass


@cli.command("retrofit")
@click.argument("package")
def cmd_retrofit(package: str) -> None:
    pdir = SPECS / package
    spec_path = pdir / f"{Path(package).name}.spec"
    if not spec_path.exists():
        cands = list(pdir.glob("*.spec"))
        if len(cands) != 1:
            raise SystemExit(f"cannot pick spec in {pdir}: {[c.name for c in cands]}")
        spec_path = cands[0]
    original = spec_path.read_text()
    try:
        text, notes = retrofit_spec(spec_path)
        spec_path.write_text(text)
    except BaseException:
        spec_path.write_text(original)
        raise
    for n in notes:
        click.echo(n)
    click.echo(f"retrofitted {spec_path}")


def spec_version(text: str) -> str:
    m = re.search(r"(?m)^Version:\s+(\S+)", text)
    return m.group(1) if m else ""


def spec_source0(text: str) -> str | None:
    m = re.search(r"(?m)^Source0?:\s+(\S+)", text)
    return m.group(1) if m else None


def refresh_spec(spec_path: Path) -> list[str]:
    text = spec_path.read_text()
    if "# oreon:deps" not in text:
        return []
    facts = spec_facts(spec_path)
    if facts["lang"] != "python":
        return ["deps vendored or physical, nothing to refresh"]
    man = pyproject_manifest(facts)
    version = facts["version"]
    url = spec_source0(text) or facts["source0_url"]
    m = re.search(r"(?ms)^(# oreon:deps\n)(.*?)(# /oreon:deps\n)", text)
    if not m:
        return ["no deps block"]
    block_lines = [l for l in m.group(2).splitlines() if not re.match(r"(?i)^(BuildRequires|Requires):", l)]
    brs = sorted(set(man["build"]) | {"python3-devel", "pyproject-rpm-macros"})
    new_block = [f"BuildRequires:  {d}" for d in brs]
    new_block.extend(block_lines)
    payload = facts["payload"]
    new_block.append(f"Requires:       {payload} = %{{version}}-%{{release}}")
    text = text[: m.start(2)] + "\n".join(new_block) + "\n" + text[m.end(2) :]
    m = re.search(r"(?ms)^(# oreon:reqs\n)(.*?)(# /oreon:reqs\n)", text)
    if m:
        reqs = [f"Requires:       {d}" for d in dedupe_caps(sorted(set(man["run"])))]
        text = text[: m.start(2)] + "\n".join(reqs) + "\n" + text[m.end(2) :]
    spec_path.write_text(text)
    return [f"refreshed deps for {version}: {brs}"]


@cli.command("refresh")
@click.argument("package")
def cmd_refresh(package: str) -> None:
    pdir = SPECS / package
    spec_path = pdir / f"{package}.spec"
    if not spec_path.exists():
        cands = list(pdir.glob("*.spec"))
        if len(cands) != 1:
            raise SystemExit(f"cannot pick spec in {pdir}")
        spec_path = cands[0]
    for n in refresh_spec(spec_path):
        click.echo(n)


@cli.command("verify")
@click.argument("package")
@click.option("--jobs", type=int, default=4)
def cmd_verify(package: str, jobs: int) -> None:
    sys.path.insert(0, str(Path(__file__).parent))
    import orbscli
    import plancmd

    ctx = plancmd.Context(orbscli, "oreon-11-rp1", None, "https://repo-us.oreonhq.com", ("x86_64", "aarch64"), None, jobs, quiet=True)
    if package not in ctx.specs:
        raise SystemExit(f"{package} not in tree")
    ctx.specs.load_many([package], jobs)
    ctx.specs.deepen_many([package], jobs)
    spec = ctx.specs[package]
    if spec.error:
        raise SystemExit(f"spec error: {spec.error}")
    planner = ctx.planner(dynamic=False)
    reqs = planner.own_requirements(spec, dynamic=False)
    owners, missing = planner.resolve_spec(spec, reqs)
    click.echo(f"forward: {len(owners)} providers, missing {sorted(missing) or 'none'}")
    for m_item in sorted(missing):
        click.echo(f"  no provider: {m_item}")
    provs = set()
    import rpm as _rpm

    for pkg in _rpm.spec(str(spec.path)).packages:
        ph = getattr(pkg, "header", pkg)
        for p in ph[_rpm.RPMTAG_PROVIDES] or []:
            provs.add(re.split(r"\s", p, maxsplit=1)[0])
    provs = {p for p in provs if not p.startswith("rpmlib(")}
    consumers = {}
    if provs:
        pat = "(" + "|".join(re.escape(p) for p in provs) + ")"
        r = subprocess.run(["rg", "-l", "--no-messages", "-i", f"^(Build)?Requires:.*{pat}", str(SPECS)], capture_output=True, text=True)
        for f in r.stdout.splitlines():
            pname = Path(f).parent.name
            if pname != package:
                consumers[pname] = True
    click.echo(f"reverse: {len(provs)} provides, consumers: {sorted(consumers) or 'none in tree'}")
    if missing:
        sys.exit(1)


@cli.command("check")
@click.argument("package")
def cmd_check(package: str) -> None:
    pdir = SPECS / package
    spec_path = pdir / f"{package}.spec"
    if not spec_path.exists():
        cands = list(pdir.glob("*.spec"))
        if len(cands) != 1:
            raise SystemExit(f"cannot pick spec in {pdir}")
        spec_path = cands[0]
    r = subprocess.run(["rpmspec", "-P", str(spec_path)], capture_output=True, text=True)
    if r.returncode != 0:
        click.echo(r.stderr)
        sys.exit(1)
    click.echo("expands ok")


def _retrofit_worker(n: str) -> tuple[str, str | None]:
    pdir = SPECS / n
    cands = list(pdir.glob("*.spec"))
    if len(cands) != 1:
        return n, f"{len(cands)} spec files"
    specf = cands[0]
    original = specf.read_text()
    try:
        text, _notes = retrofit_spec(specf)
        tmp = specf.with_name(specf.name + ".orespec-tmp")
        tmp.write_text(text)
        tmp.replace(specf)
        return n, None
    except BaseException as e:
        try:
            specf.write_text(original)
        except Exception:
            pass
        return n, f"{type(e).__name__} {e}"


@cli.command("retrofit-all")
@click.option("--limit", type=int, default=0)
@click.option("--batch", type=int, default=80)
@click.option("--jobs", type=int, default=4)
@click.option("--from-file", type=str, default="")
def cmd_retrofit_all(limit: int, batch: int, jobs: int, from_file: str) -> None:
    import fcntl
    import socket

    socket.setdefaulttimeout(25)
    state = Path(from_file or Path.home() / ".cache" / "orespec-src" / "retrofit-done.txt")
    state.parent.mkdir(parents=True, exist_ok=True)
    lockf = open(state.with_suffix(".lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("retrofit-all already running")
    done = set(state.read_text().split()) if state.exists() else set()
    names = sorted(d.name for d in SPECS.iterdir() if d.is_dir() and d.name not in done)
    if limit:
        names = names[:limit]
    ok = fail = 0
    failed = []
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    jobs = min(max(1, jobs), 3)
    with ctx.Pool(jobs) as pool:
        for i in range(0, len(names), batch):
            chunk = names[i : i + batch]
            for n, err in pool.imap_unordered(_retrofit_worker, chunk):
                if err:
                    click.echo(f"FAIL {n}: {err}")
                    failed.append(f"{n}\t{err}")
                    fail += 1
                else:
                    ok += 1
                    done.add(n)
            state.write_text("\n".join(sorted(done)))
            state.with_suffix(".failed.txt").write_text("\n".join(failed) + "\n")
            click.echo(f"progress ok={ok} fail={fail} last={chunk[-1]}")
    click.echo(f"ok={ok} fail={fail}")
    if failed:
        click.echo(f"failed list: {state.with_suffix('.failed.txt')}")


if __name__ == "__main__":
    cli()
