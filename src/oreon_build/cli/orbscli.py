# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import stat
import sys
import tarfile
import threading
import time
import zipfile
import shutil
import subprocess
import tempfile
import asyncio
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import click
import httpx

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "orbscli"
CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"
CONFIG_PATH = CONFIG_DIR / "config.json"

_status_lock = threading.Lock()
_status_live = False
_big_tree_lock = threading.Lock()
_unpack_sem = threading.Semaphore(2)


def _status_line(msg: str) -> None:
    global _status_live
    with _status_lock:
        sys.stderr.write("\r\033[K%s" % msg)
        sys.stderr.flush()
        _status_live = True


def _status_nl() -> None:
    global _status_live
    with _status_lock:
        if _status_live:
            sys.stderr.write("\n")
            sys.stderr.flush()
            _status_live = False

def _mem_available_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return 4096


def _host_workers(kind: str) -> int:
    mem = _mem_available_mb()
    cpu = os.cpu_count() or 4
    if kind == "net":
        return 16
    if kind == "unpack":
        if mem < 2048:
            return 1
        return 2
    if mem < 2048:
        return 2
    return max(4, min(cpu, 8))


def _iter_bounded(max_workers: int, fn, items, inflight: int | None = None):
    cap = inflight or max(1, max_workers)
    it = iter(items)
    pending: set[Any] = set()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:

        def pump() -> None:
            while len(pending) < cap:
                try:
                    item = next(it)
                except StopIteration:
                    return
                pending.add(pool.submit(fn, item))

        pump()
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            pending = set(pending)
            for fut in done:
                yield fut.result()
            pump()


def _current_ctx() -> click.Context | None:
    return click.get_current_context(silent=True)

def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def _save_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_config_dir()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

def get_api_url() -> str:
    ctx = _current_ctx()
    if ctx and ctx.obj.get("API_URL"):
        return str(ctx.obj["API_URL"]).rstrip("/")
    env = (os.environ.get("ORBSCLI_API_URL") or os.environ.get("OREON_API_URL") or "").strip()
    if env:
        return env.rstrip("/")
    cfg = _load_json(CONFIG_PATH)
    return str(cfg.get("api_url", "https://build.oreonhq.com")).rstrip("/")

def get_watchdog_url() -> str:
    ctx = _current_ctx()
    if ctx and ctx.obj.get("WATCHDOG_URL"):
        return str(ctx.obj["WATCHDOG_URL"]).rstrip("/")
    env = os.environ.get("ORBSCLI_WATCHDOG_URL", "").strip()
    if env:
        return env.rstrip("/")
    cfg = _load_json(CONFIG_PATH)
    return str(cfg.get("watchdog_url", "http://localhost:8001")).rstrip("/")

def get_token() -> str | None:
    cred = _load_json(CREDENTIALS_PATH)
    t = (cred.get("access_token") or "").strip()
    if t:
        return t
    return (os.environ.get("ORBSCLI_TOKEN") or os.environ.get("OREON_TOKEN") or "").strip() or None

def _jwt_payload_unverified(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}

def jwt_role(token: str) -> str:
    return str(_jwt_payload_unverified(token).get("role") or "")

def require_auth(token: str | None) -> str:
    if not token:
        click.echo("Not logged in. Run: orbscli login ...", err=True)
        sys.exit(2)
    return token

def require_maintainer(token: str) -> None:
    role = jwt_role(token)
    if role not in ("admin", "maintainer"):
        click.echo("Maintainer or admin role required (your role: %s)." % (role or "unknown"), err=True)
        sys.exit(3)

def require_admin(token: str) -> None:
    role = jwt_role(token)
    if role != "admin":
        click.echo("Admin role required (your role: %s)." % (role or "unknown"), err=True)
        sys.exit(3)

def http_request_raw(
    method: str,
    base: str,
    path: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | list[Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> httpx.Response:
    url = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=timeout) as client:
        return client.request(method, url, headers=headers, json=json_body, params=params)

def http_json(
    method: str,
    base: str,
    path: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | list[Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> Any:
    r = http_request_raw(method, base, path, token=token, json_body=json_body, params=params, timeout=timeout)
    if r.status_code == 204:
        return None
    if not r.is_success:
        detail = ""
        try:
            detail = str(r.json().get("detail", r.text))
        except Exception:
            detail = r.text[:500]
        click.echo("HTTP %s %s\n%s" % (r.status_code, path, detail), err=True)
        sys.exit(1)
    if r.headers.get("content-type", "").startswith("application/json"):
        return r.json()
    return r.text

def parse_package_names(args: tuple[str, ...]) -> list[str]:
    joined = " ".join(args).strip()
    if not joined:
        return []
    parts = re.split(r"[\s,]+", joined)
    return [p for p in parts if p]

def parse_package_blob(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\s,]+", (text or "").strip()):
        if not part or part in seen:
            continue
        seen.add(part)
        out.append(part)
    return out

_RE_SPEC_VERSION = re.compile(r"(?im)^(?P<pre>\s*Version\s*:\s*)(?P<val>\S+)(?P<post>.*)$")
_RE_SPEC_RELEASE = re.compile(r"(?im)^(?P<pre>\s*Release\s*:\s*)(?P<num>\d+)(?P<rest>.*)$")
_RE_SPEC_DEFINE = re.compile(
    r"(?im)^(?P<pre>\s*%(?:global|define)\s+)(?P<name>[A-Za-z_][\w]*)(?P<sp>\s+)(?P<val>\S+)(?P<post>.*)$"
)
_RE_VERSION_MACRO = re.compile(r"^%\{([A-Za-z_][\w]*)\}$")

def _split_spec_changelog(text: str) -> tuple[str, str]:
    m = re.search(r"(?im)^%changelog\b", text)
    if not m:
        return text, ""
    return text[: m.start()], text[m.start() :]

def _define_literal(preamble: str, name: str) -> str | None:
    want = (name or "").strip().lower()
    if not want:
        return None
    found: str | None = None
    for m in _RE_SPEC_DEFINE.finditer(preamble):
        if m.group("name").lower() != want:
            continue
        val = (m.group("val") or "").strip()
        if val:
            found = val
    return found

def _expand_spec_version_token(preamble: str, val: str) -> str | None:
    cur = (val or "").strip()
    seen: set[str] = set()
    for _ in range(8):
        if not cur or cur in seen:
            return None
        seen.add(cur)
        if "%" not in cur:
            return cur
        mm = _RE_VERSION_MACRO.fullmatch(cur)
        if not mm:
            return None
        nxt = _define_literal(preamble, mm.group(1))
        if not nxt:
            return None
        cur = nxt
    return None

def _spec_literal_version(preamble: str) -> tuple[str, str] | None:
    vm = _RE_SPEC_VERSION.search(preamble)
    if not vm:
        return None
    val = vm.group("val").strip()
    if not val:
        return None
    if "%" in val:
        dval = _expand_spec_version_token(preamble, val)
        if not dval or "%" in dval:
            return None
        return dval, "macro"
    return val, "tag"

def _dotted_prefixes(ver: str) -> dict[int, str]:
    parts = [p for p in (ver or "").split(".") if p != ""]
    out: dict[int, str] = {}
    for n in range(2, len(parts)):
        out[n] = ".".join(parts[:n])
    return out


def _bump_preserve_define(name: str) -> bool:
    # kabi dumps are separate artifacts. rewriting kabiversion with the
    # kernel version makes Source300/301 point at files that do not exist yet.
    n = (name or "").lower()
    return n.startswith("kabi")


def _rewrite_version_defines(preamble: str, old: str, new: str) -> str:
    old_px = _dotted_prefixes(old)
    new_px = _dotted_prefixes(new)

    def _sub(m: re.Match[str]) -> str:
        if _bump_preserve_define(m.group("name")):
            return m.group(0)
        val = (m.group("val") or "").strip()
        if val == old:
            val = new
        else:
            for n, px in old_px.items():
                if val == px and n in new_px:
                    val = new_px[n]
                    break
        return m.group("pre") + m.group("name") + m.group("sp") + val + m.group("post")

    return _RE_SPEC_DEFINE.sub(_sub, preamble)


def _replace_version_token(text: str, old: str, new: str) -> str:
    if old == new:
        return text
    if len(old) < 3 and "." not in old:
        return text
    rx = re.compile(r"(?<![0-9A-Za-z.])" + re.escape(old) + r"(?!(?:[0-9A-Za-z]|\.\d))")
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        dm = _RE_SPEC_DEFINE.match(line)
        if dm and _bump_preserve_define(dm.group("name")):
            out.append(line)
            continue
        out.append(rx.sub(new, line))
    return "".join(out)

def bump_spec_to_version(text: str, latest: str) -> tuple[str, str, str] | None:
    latest = (latest or "").strip()
    if not latest or "%" in latest:
        return None
    pre, log = _split_spec_changelog(text)
    found = _spec_literal_version(pre)
    if not found:
        return None
    old, kind = found
    if old == latest:
        return text, old, latest
    if kind == "macro":
        pre = _rewrite_version_defines(pre, old, latest)
    else:
        pre = _RE_SPEC_VERSION.sub(lambda m: m.group("pre") + latest + m.group("post"), pre, count=1)
        pre = _rewrite_version_defines(pre, old, latest)
    pre = _RE_SPEC_RELEASE.sub(lambda m: m.group("pre") + "1" + m.group("rest"), pre, count=1)
    pre = _replace_version_token(pre, old, latest)
    old_maj = old.split(".", 1)[0]
    new_maj = latest.split(".", 1)[0]
    if old_maj != new_maj and old_maj.isdigit() and new_maj.isdigit():
        pre = pre.replace("v%s.x" % old_maj, "v%s.x" % new_maj)
    return pre + log, old, latest


def _spec_macro_line(text: str, name: str) -> str | None:
    return _define_literal(_split_spec_changelog(text)[0], name)


def _lang2rpm_kind(pkg: str, text: str) -> str | None:
    if re.search(r"(?i)Generated by rust2rpm", text) or _spec_macro_line(text, "crate"):
        if pkg in {"rust", "rust-packaging", "cargo", "cargo2rpm"}:
            return None
        return "rust"
    if re.search(r"(?i)Generated by go2rpm", text) or _spec_macro_line(text, "goipath"):
        return "go"
    if re.search(r"(?i)Generated by pyp2rpm|Generated by pyp2spec", text):
        return "python"
    if pkg.startswith("python-") and (
        "%pypi_source" in text
        or "%{pypi_source" in text
        or _spec_macro_line(text, "srcname")
        or _spec_macro_line(text, "pypi_name")
        or "%pyproject_buildrequires" in text
        or "%pyproject_wheel" in text
        or "pypi.org" in text.lower()
        or "files.pythonhosted.org" in text.lower()
    ):
        return "python"
    if pkg.startswith("rubygem-") or _spec_macro_line(text, "gem_name"):
        return "ruby"
    if pkg.startswith("nodejs-") or _spec_macro_line(text, "npm_name"):
        return "node"
    if pkg.startswith("perl-") and (
        re.search(r"(?i)Generated by cpan2rpm|Generated by cpanspec", text)
        or "cpan.org" in text.lower()
        or "metacpan.org" in text.lower()
    ):
        return "perl"
    if pkg.startswith("R-") or _spec_macro_line(text, "R_pkg") or re.search(r"(?i)Generated by r2spec", text):
        if pkg in {"R", "R-java", "R-core"}:
            return None
        return "r"
    if pkg.startswith("ghc-") and pkg not in {"ghc", "ghc-rpm-macros"}:
        return "haskell"
    if (
        re.search(r"(?i)xmvn|mvn\(|%mvn_|javapackages-local", text)
        and not pkg.startswith("javapackages-")
        and any(shutil.which(name) for name in _lang2rpm_bins("java"))
    ):
        return "java"
    if pkg.startswith("php-") and (
        "pear.php.net" in text.lower() or _spec_macro_line(text, "pear_name")
    ):
        return "php"
    return None


def _lang2rpm_bins(kind: str) -> list[str]:
    return {
        "rust": ["rust2rpm"],
        "go": ["go2rpm"],
        "python": ["pyp2spec", "pyp2rpm"],
        "ruby": ["gem2rpm"],
        "node": ["npm2rpm"],
        "perl": ["cpanspec"],
        "r": ["R2spec", "R2rpm"],
        "java": ["xmvn-new-javapackage", "xmvn-generate"],
        "haskell": ["cabal-rpm", "cblrpm"],
        "php": ["pear", "pear2rpm"],
    }.get(kind, [])


def _lang2rpm_rpms_for_kind(kind: str) -> list[str]:
    return {
        "rust": ["rust2rpm"],
        "go": ["go2rpm"],
        "python": ["pyp2spec", "pyp2rpm"],
        "ruby": ["rubygem-gem2rpm"],
        "perl": ["cpanspec"],
        "r": ["R2spec"],
        "java": [],
        "haskell": ["cabal-rpm"],
        "php": ["php-pear-PEAR-Command-Packaging"],
    }.get(kind, [])


def _lang2rpm_all_bins() -> list[str]:
    seen: list[str] = []
    for kind in (
        "rust",
        "go",
        "python",
        "ruby",
        "node",
        "perl",
        "r",
        "java",
        "haskell",
        "php",
    ):
        for name in _lang2rpm_bins(kind):
            if name not in seen:
                seen.append(name)
    if "nvchecker" not in seen:
        seen.append("nvchecker")
    return seen


def _dnf_host_bin() -> str | None:
    return shutil.which("dnf5") or shutil.which("dnf")


def _dnf_sudo_prefix(ask_pass: bool) -> list[str]:
    if os.geteuid() == 0:
        return []
    sudo = shutil.which("sudo")
    if sudo is None:
        return []
    if ask_pass:
        return [sudo]
    return [sudo, "-n"]


def _dnf_run(args: list[str], ask_pass: bool) -> subprocess.CompletedProcess[str]:
    dnf = _dnf_host_bin()
    if dnf is None:
        raise click.ClickException("dnf not on PATH")
    prefix = _dnf_sudo_prefix(ask_pass)
    if os.geteuid() != 0 and not prefix:
        raise click.ClickException("need root or sudo to install translator rpms")
    return subprocess.run(prefix + [dnf, *args], text=True)


def _dnf_repoquery_names(names: list[str]) -> list[str]:
    if not names:
        return []
    dnf = _dnf_host_bin()
    if dnf is None:
        return []
    proc = subprocess.run(
        [
            dnf,
            "repoquery",
            "-q",
            "--available",
            "--queryformat",
            "%{name}\n",
            *names,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    found: list[str] = []
    for line in (proc.stdout or "").splitlines():
        name = line.strip()
        if name and name in names and name not in found:
            found.append(name)
    return found


def _dnf_install_host(pkgs: list[str]) -> bool:
    if not pkgs:
        return True
    click.echo("dnf install %s" % " ".join(pkgs), err=True)
    args = ["install", "-y", *pkgs]
    proc = _dnf_run(args, ask_pass=False)
    if proc.returncode != 0 and os.geteuid() != 0 and sys.stdin.isatty():
        click.echo("sudo needs a password, retrying", err=True)
        proc = _dnf_run(args, ask_pass=True)
    return proc.returncode == 0


def _pip_install_nvchecker() -> None:
    localbin = str(Path.home() / ".local" / "bin")
    path = os.environ.get("PATH", "")
    if localbin not in path.split(os.pathsep):
        os.environ["PATH"] = localbin + os.pathsep + path
    if shutil.which("nvchecker"):
        return
    click.echo("pip install nvchecker", err=True)
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--user", "nvchecker[pypi]"],
    )
    if proc.returncode != 0 or shutil.which("nvchecker") is None:
        raise click.ClickException("nvchecker install failed")


def _lang2rpm_ensure_host_tools() -> None:
    want_rpms: list[str] = []
    missing_kinds: list[str] = []
    for kind in (
        "rust",
        "go",
        "python",
        "ruby",
        "perl",
        "r",
        "java",
        "haskell",
        "php",
    ):
        if any(shutil.which(name) for name in _lang2rpm_bins(kind)):
            continue
        rpms = _lang2rpm_rpms_for_kind(kind)
        if not rpms:
            continue
        missing_kinds.append(kind)
        for rpm in rpms:
            if rpm not in want_rpms:
                want_rpms.append(rpm)
    if want_rpms:
        click.echo("installing translators for %s" % " ".join(missing_kinds), err=True)
        if not _dnf_install_host(want_rpms):
            raise click.ClickException(
                "dnf install failed for %s" % " ".join(want_rpms)
            )
    leftover = [
        kind
        for kind in missing_kinds
        if not any(shutil.which(name) for name in _lang2rpm_bins(kind))
    ]
    if leftover:
        raise click.ClickException(
            "translators still missing after dnf install: %s" % " ".join(leftover)
        )
    _pip_install_nvchecker()


def _lang2rpm_upstream_name(kind: str, pkg: str, text: str) -> str | None:
    if kind == "rust":
        return _spec_macro_line(text, "crate") or pkg.removeprefix("rust-")
    if kind == "go":
        return _spec_macro_line(text, "goipath")
    if kind == "python":
        return (
            _spec_macro_line(text, "srcname")
            or _spec_macro_line(text, "pypi_name")
            or pkg.removeprefix("python-")
        )
    if kind == "ruby":
        return _spec_macro_line(text, "gem_name") or pkg.removeprefix("rubygem-")
    if kind == "node":
        return _spec_macro_line(text, "npm_name") or pkg.removeprefix("nodejs-")
    if kind == "perl":
        src = _spec_macro_line(text, "mod_name") or pkg.removeprefix("perl-")
        return src.replace("-", "::")
    if kind == "r":
        return _spec_macro_line(text, "R_pkg") or pkg.removeprefix("R-")
    if kind == "haskell":
        return _spec_macro_line(text, "pkg_name") or pkg.removeprefix("ghc-")
    if kind == "java":
        return _spec_macro_line(text, "pkg_name") or pkg
    if kind == "php":
        return _spec_macro_line(text, "pear_name") or pkg.removeprefix("php-")
    return None


def _spec_package_name_from_text(text: str) -> str | None:
    match = _RE_SPEC_NAME.search(_split_spec_changelog(text)[0])
    return match.group(1).strip() if match else None


def _lang2rpm_argv(
    kind: str, bin_name: str, upstream: str, latest: str, pkg: str, text: str
) -> list[str]:
    if kind == "rust":
        argv = [bin_name, "--no-existence-check", "--no-auto-changelog-entry"]
        if re.search(r"(?im)^Patch\d*\s*:", text):
            argv.append("--reuse-patch")
        name = _spec_package_name_from_text(text) or pkg
        crate = _spec_macro_line(text, "crate") or upstream
        if name != "rust-%s" % crate:
            argv.append("--compat")
        argv.append("%s@%s" % (crate, latest))
        return argv
    if kind == "go":
        return [bin_name, "--no-auto-changelog-entry", "-v", latest, upstream]
    if kind == "python":
        exe = Path(bin_name).name
        if exe == "pyp2spec":
            return [bin_name, "-v", latest, "-a", upstream]
        return [bin_name, "-v", latest, "-r", pkg, upstream]
    if kind == "ruby":
        return [bin_name, "-v", latest, upstream]
    if kind == "node":
        return [bin_name, "-v", latest, upstream]
    if kind == "perl":
        if bin_name == "cpanspec":
            return [bin_name, "-f", "--version", latest, upstream]
        return [bin_name, "--spec-only", "--version", latest, upstream]
    if kind == "r":
        if bin_name.lower() in {"r2spec"}:
            return [bin_name, "-f", "--version", latest, upstream]
        return [bin_name, upstream]
    if kind == "haskell":
        return [bin_name, "spec", "-l", latest, upstream]
    if kind == "java":
        return [bin_name, upstream]
    if kind == "php":
        return [bin_name, upstream]
    return [bin_name, upstream]


def _lang2rpm_pick_spec(tmp: Path, spec_path: Path) -> Path | None:
    hits = sorted(tmp.glob("*.spec"))
    if not hits:
        return None
    for hit in hits:
        if hit.name == spec_path.name:
            return hit
    if len(hits) == 1:
        return hits[0]
    return None


def _lang2rpm_merge(old_text: str, new_text: str) -> str:
    new_pre, _ = _split_spec_changelog(new_text)
    old_pre, old_log = _split_spec_changelog(old_text)
    if not re.search(r"(?im)^Patch\d*\s*:", new_pre):
        patches = [
            m.group(0).rstrip()
            for m in re.finditer(r"(?im)^Patch\d*\s*:.*$", old_pre)
        ]
        if patches:
            desc = re.search(r"(?im)^%description\b", new_pre)
            blob = "\n".join(patches) + "\n\n"
            if desc:
                new_pre = new_pre[: desc.start()] + blob + new_pre[desc.start() :]
            else:
                new_pre = new_pre.rstrip() + "\n" + blob
    merged = (new_pre.rstrip() + "\n\n" + old_log) if old_log else new_pre
    if "%global source0_hash" not in merged and "%global source0_hash" in old_text:
        merged = _set_source_hash_guard(merged, "none")
    return merged


def _lang2rpm_run(
    kind: str, spec_path: Path, text: str, latest: str
) -> tuple[str | None, str | None]:
    upstream = _lang2rpm_upstream_name(kind, spec_path.parent.name, text)
    if not upstream:
        return None, "cannot find upstream name for %s translator" % kind
    bins = [name for name in _lang2rpm_bins(kind) if shutil.which(name)]
    if not bins:
        return None, "missing-translator"
    spec_dir = spec_path.parent
    last_err = "translator failed"
    for bin_name in bins:
        argv = _lang2rpm_argv(kind, bin_name, upstream, latest, spec_path.parent.name, text)
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            for name in ("rust2rpm.toml", "rust2rpm.conf", "go-vendor-tools.toml"):
                src = spec_dir / name
                if src.is_file():
                    shutil.copy2(src, tmp / name)
            for pat in ("*.diff", "*.patch"):
                for src in spec_dir.glob(pat):
                    shutil.copy2(src, tmp / src.name)
            shutil.copy2(spec_path, tmp / spec_path.name)
            run_argv = list(argv)
            if kind == "python" and Path(bin_name).name == "pyp2spec":
                run_argv = argv[:1] + ["-o", str(tmp / spec_path.name)] + argv[1:]
            if kind == "rust":
                run_argv = argv[:1] + ["-o", str(tmp)] + argv[1:]
            tries = [run_argv]
            if kind == "rust":
                tries.append([bin_name, "-o", str(tmp), "%s@%s" % (upstream, latest)])
            proc = None
            for attempt in tries:
                proc = subprocess.run(attempt, cwd=tmp, capture_output=True, text=True)
                if proc.returncode == 0:
                    break
            if proc is None or proc.returncode != 0:
                last_err = ((proc.stderr if proc else "") or (proc.stdout if proc else "") or last_err).strip().splitlines()
                last_err = last_err[-1][:300] if last_err else "translator failed"
                continue
            out_spec = _lang2rpm_pick_spec(tmp, spec_path)
            body = ""
            if out_spec and out_spec.is_file():
                body = out_spec.read_text(encoding="utf-8", errors="replace")
            stdout = proc.stdout or ""
            if len(stdout.strip()) > 40 and ("Name:" in stdout or "%package" in stdout):
                body = stdout
            if not body.strip():
                last_err = "%s wrote no spec" % bin_name
                continue
            return _lang2rpm_merge(text, body), None
    return None, last_err


def _spec_bump_compute(
    pkg: str, spec_path: Path, text: str, latest: str
) -> tuple[str, str, str | None, str | None]:
    found = _spec_literal_version(_split_spec_changelog(text)[0])
    old_ver = found[0] if found else None
    if not latest:
        return "fail", "no version from checker", None, None
    if old_ver == latest:
        return "skip", "current", old_ver, latest
    if old_ver and _ver_rank(latest) < _ver_rank(old_ver):
        return "skip", "not newer", old_ver, latest
    if (
        old_ver
        and re.search(r"\d+\.\d+$", pkg)
        and old_ver.split(".", 1)[0] != latest.split(".", 1)[0]
    ):
        return "skip", "compat", old_ver, latest
    kind = _lang2rpm_kind(pkg, text)
    if kind:
        new_text, err = _lang2rpm_run(kind, spec_path, text, latest)
        if err or not new_text:
            return "fail", err or "translator produced empty spec", old_ver, latest
        return "ok", new_text, old_ver, latest
    bumped = bump_spec_to_version(text, latest)
    if bumped is None:
        return "fail", "no literal Version", old_ver, latest
    new_text, old, new = bumped
    if old == new:
        return "skip", "current", old, new
    if new_text == text:
        return "fail", "spec text unchanged", old, new
    return "ok", new_text, old, new


_RE_SPEC_SOURCE = re.compile(
    r"(?im)^(?P<pre>\s*Source(?P<num>\d*)\s*:\s*)(?P<val>\S.*?)(?P<post>\s*(?:#.*)?)$"
)
_RE_SPEC_PATCH = re.compile(
    r"(?im)^(?P<pre>\s*Patch(?P<num>\d*)\s*:\s*)(?P<val>\S.*?)(?P<post>\s*(?:#.*)?)$"
)
_RE_CUT_MACRO = re.compile(r"^%\(echo\s+(.*?)\s*\|\s*cut\s+-d\.\s+-f([\d,\-]+)\)$")
_RE_NUMERIC_CONDITION = re.compile(r"^(-?\d+)\s*(>=|<=|>|<|==|!=)\s*(-?\d+)$")


def _select_dot_fields(value: str, fields: str) -> str | None:
    """Implement ``cut -d. -f`` field selection (1-based) for macro snippets."""
    token = (value or "").strip().split()[0] if (value or "").strip() else ""
    parts = token.split(".") if token else []
    if not parts:
        return None
    selected: list[str] = []
    for chunk in fields.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            try:
                start, end = (int(item) for item in chunk.split("-", 1))
            except ValueError:
                return None
            selected.extend(parts[start - 1 : end])
        else:
            try:
                selected.append(parts[int(chunk) - 1])
            except (ValueError, IndexError):
                return None
    return ".".join(selected)


def _eval_cut_macro(raw: str, values: dict[str, str]) -> str | None:
    """Evaluate ``%(echo %{version} | cut -d. -f...)`` snippets offline."""
    match = _RE_CUT_MACRO.match((raw or "").strip())
    if match is None:
        return None
    inner = re.sub(
        r"%\{([^}?]+)\}",
        lambda item: values.get(item.group(1), item.group(0)),
        match.group(1),
    )
    if "%{" in inner or "%(" in inner:
        return None
    return _select_dot_fields(inner, match.group(2))


def _eval_numeric_condition(raw: str) -> bool | None:
    """Evaluate a substituted ``%if`` numeric comparison, if possible."""
    match = _RE_NUMERIC_CONDITION.match((raw or "").strip())
    if match is None:
        return None
    left, operator, right = int(match.group(1)), match.group(2), int(match.group(3))
    return {
        ">=": left >= right,
        "<=": left <= right,
        ">": left > right,
        "<": left < right,
        "==": left == right,
        "!=": left != right,
    }[operator]


def _expand_rpm_cond_macros(raw: str, macros: dict[str, str]) -> str:
    def repl_opt(match: re.Match[str]) -> str:
        return macros.get(match.group(1), "")

    def repl_plain(match: re.Match[str]) -> str:
        return macros.get(match.group(1), match.group(0))

    out = re.sub(r"%\{\?([^}]+)\}", repl_opt, raw or "")
    out = re.sub(r"%\{([^}?]+)\}", repl_plain, out)
    return out


def _split_top_level(expr: str, op: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and expr.startswith(op, i):
            parts.append(expr[start:i].strip())
            i += len(op)
            start = i
            continue
        i += 1
    parts.append(expr[start:].strip())
    return parts


def _eval_rpm_bool_expr(expr: str) -> bool:
    s = re.sub(r"\s+", " ", (expr or "").strip())
    if not s:
        return True
    parts = _split_top_level(s, "||")
    if len(parts) > 1:
        return any(_eval_rpm_bool_expr(part) for part in parts)
    parts = _split_top_level(s, "&&")
    if len(parts) > 1:
        return all(_eval_rpm_bool_expr(part) for part in parts)
    if s.startswith("!"):
        return not _eval_rpm_bool_expr(s[1:].strip())
    if s.startswith("(") and s.endswith(")"):
        return _eval_rpm_bool_expr(s[1:-1])
    decided = _eval_numeric_condition(s)
    if decided is not None:
        return decided
    if re.fullmatch(r"-?\d+", s):
        return int(s) != 0
    raise ValueError(s)


def _spec_cond_truth(cond: str, macros: dict[str, str] | None = None) -> bool | None:
    raw = re.sub(r"#.*", "", cond or "").strip()
    if not raw:
        return True
    macros = macros or {}
    expanded = _expand_rpm_cond_macros(raw, macros)
    if "%{" not in expanded and "%(" not in expanded:
        try:
            return _eval_rpm_bool_expr(expanded)
        except ValueError:
            pass
    return _spec_if_truth(raw)


def _collect_spec_macros(text: str, base: dict[str, str]) -> dict[str, str]:
    """Collect ``%global``/``%define`` values honoring ``%if`` branches.

    Plain scanning misses shell ``cut`` snippets (used by specs such as
    ``kwin-x11`` for parts of ``Source0``) and takes the wrong side of
    ``%if %{revision} >= 50`` blocks. Unknown conditions stay active so
    behavior degrades to the old last-wins collection.
    """
    macros: dict[str, str] = {}
    active: list[bool] = [True]

    def substitute(raw: str) -> str:
        for _ in range(10):
            expanded = _expand_rpm_cond_macros(raw, {**base, **macros})
            if expanded == raw:
                break
            raw = expanded
        return raw

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("%if"):
            cond = stripped.split(None, 1)[1] if len(stripped.split(None, 1)) > 1 else "1"
            decided = _spec_cond_truth(cond, {**base, **macros})
            active.append(active[-1] if decided is None else (active[-1] and decided))
        elif stripped == "%else":
            if len(active) > 1:
                active[-1] = active[-2] and not active[-1]
        elif stripped == "%endif":
            if len(active) > 1:
                active.pop()
        elif all(active) and re.match(r"^%(?:global|define)\s", stripped):
            words = stripped.split(None, 2)
            if len(words) != 3:
                continue
            evaluated = _eval_cut_macro(words[2], {**base, **macros})
            macros[words[1]] = evaluated if evaluated is not None else substitute(words[2])
    return macros


def _spec_source_macros(text: str, version: str, name: str) -> dict[str, str]:
    """Return macro values usable for expanding ``Source`` URLs."""
    preamble, _ = _split_spec_changelog(text)
    # oreon tree parse defaults. no fedora so RHEL/centos-only Sources stay gated off.
    macros: dict[str, str] = {
        "version": version,
        "name": name,
        "oreon": "11",
        "nil": "",
        "dist": ".oreon11",
    }
    url_match = re.search(r"(?im)^\s*URL\s*:\s*(\S+)", preamble)
    if url_match is not None:
        macros["url"] = url_match.group(1).strip()
    macros.update(_collect_spec_macros(preamble, macros))
    macros["version"] = version
    macros["name"] = name
    macros.setdefault("oreon", "11")
    return macros


def _expand_source_macros(raw: str, macros: dict[str, str]) -> str:
    expanded = raw
    for _ in range(10):
        nxt = re.sub(
            r"%\{([A-Za-z0-9_]+)\}",
            lambda item: macros.get(item.group(1), item.group(0)),
            expanded,
        )
        nxt = re.sub(
            r"%(name|version|url)\b",
            lambda item: macros.get(item.group(1), item.group(0)),
            nxt,
        )
        if nxt == expanded:
            break
        expanded = nxt
    return expanded


def _spec_http_sources(text: str, macros: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return ``(number, raw, expanded)`` for each remote HTTP(S) Source.

    Honors ``%if``/``%else``/``%endif`` so Oreon-disabled Sources (centos
    kabi dumps, etc) are not verified or rewritten.
    """
    preamble, _ = _split_spec_changelog(text)
    out: list[tuple[str, str, str]] = []
    active: list[bool] = [True]
    for line in preamble.splitlines():
        stripped = line.strip()
        if stripped.startswith("%if"):
            cond = stripped.split(None, 1)[1] if len(stripped.split(None, 1)) > 1 else "1"
            decided = _spec_cond_truth(cond, macros)
            active.append(active[-1] if decided is None else (active[-1] and decided))
            continue
        if stripped.startswith("%elif"):
            if len(active) < 2:
                continue
            cond = stripped.split(None, 1)[1] if len(stripped.split(None, 1)) > 1 else "1"
            decided = _spec_cond_truth(cond, macros)
            parent = active[-2]
            active[-1] = parent if decided is None else (parent and decided)
            continue
        if stripped == "%else":
            if len(active) >= 2:
                active[-1] = active[-2] and not active[-1]
            continue
        if stripped == "%endif":
            if len(active) > 1:
                active.pop()
            continue
        if not all(active):
            continue
        match = _RE_SPEC_SOURCE.match(line)
        if not match:
            continue
        raw = match.group("val").strip()
        expanded = _expand_source_macros(raw, macros)
        if "crates_source" in raw:
            crate = macros.get("crate")
            ver = (macros.get("crate_version") or macros.get("version") or "").split("+", 1)[0]
            if crate and ver:
                expanded = "https://static.crates.io/crates/%s/%s-%s.crate" % (
                    crate,
                    crate,
                    ver,
                )
        if not expanded.lower().startswith(("http://", "https://")):
            continue
        out.append((match.group("num") or "0", raw, expanded))
    return out


def _verify_source_url(client: "httpx.Client", url: str) -> bool:
    """Return whether a Source URL is downloadable (HEAD, then ranged GET)."""
    try:
        response = client.head(url, follow_redirects=True)
        if response.status_code < 400:
            return True
        if response.status_code not in (403, 405):
            return False
        with client.stream(
            "GET", url, follow_redirects=True, headers={"Range": "bytes=0-0"}
        ) as streamed:
            return streamed.status_code < 400
    except Exception:
        return False


def _fetch_source_sha256(client: "httpx.Client", url: str) -> str | None:
    """Stream a Source URL and return its hex SHA-256 digest, if fetchable."""
    got = _fetch_source_archive(client, url, None)
    if not got:
        return None
    path, digest = got
    path.unlink(missing_ok=True)
    return digest


def _fetch_source_archive(
    client: "httpx.Client", url: str, dest: Path | None
) -> tuple[Path, str] | None:
    """Download a Source URL to dest (or a temp file) and return path plus sha256."""
    import hashlib as _hashlib
    import tempfile as _tempfile

    close_later = dest is None
    handle = None
    try:
        if dest is None:
            handle = _tempfile.NamedTemporaryFile(prefix="orbscli-src-", delete=False)
            dest = Path(handle.name)
        else:
            handle = dest.open("wb")
        digest = _hashlib.sha256()
        url = url.split("#", 1)[0]
        with client.stream("GET", url, follow_redirects=True) as response:
            if response.status_code >= 400:
                handle.close()
                if close_later:
                    dest.unlink(missing_ok=True)
                return None
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
                    digest.update(chunk)
        handle.close()
        return dest, digest.hexdigest()
    except Exception:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if close_later and dest is not None:
            dest.unlink(missing_ok=True)
        return None


def _spec_patch_files(text: str, macros: dict[str, str], spec_dir: Path) -> list[tuple[str, Path]]:
    preamble, _ = _split_spec_changelog(text)
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for match in _RE_SPEC_PATCH.finditer(preamble):
        raw = match.group("val").strip()
        expanded = _expand_source_macros(raw, macros).strip()
        if not expanded or expanded.startswith(("%", "http://", "https://")):
            continue
        name = Path(expanded).name
        if name in seen:
            continue
        seen.add(name)
        out.append((name, spec_dir / name))
    return out


def _patch_is_empty(path: Path) -> bool:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return not re.search(r"(?m)^(diff |--- |\+\+\+ )", data)


def _unpack_source_archive(archive: Path, dest: Path) -> Path | None:
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["tar", "-C", str(dest), "-xf", str(archive)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        proc = subprocess.run(
            ["unzip", "-q", "-d", str(dest), str(archive)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
    kids = [p for p in dest.iterdir() if p.name not in {".", ".."}]
    if len(kids) == 1 and kids[0].is_dir():
        return kids[0]
    return dest


def _patch_strip_works(tree: Path, patch: Path, strip: int) -> bool:
    argv = ["git", "-C", str(tree), "apply", "--check", "-p%d" % strip, str(patch)]
    if subprocess.run(argv, capture_output=True).returncode == 0:
        return True
    argv = [
        "patch", "-d", str(tree), "--dry-run", "--forward", "--batch",
        "-p%d" % strip, "-i", str(patch),
    ]
    return subprocess.run(argv, capture_output=True).returncode == 0


def _apply_patch(tree: Path, patch: Path, strip: int) -> bool:
    argv = ["git", "-C", str(tree), "apply", "-p%d" % strip, str(patch)]
    if subprocess.run(argv, capture_output=True).returncode == 0:
        return True
    argv = [
        "patch", "-d", str(tree), "--forward", "--batch",
        "-p%d" % strip, "-i", str(patch),
    ]
    return subprocess.run(argv, capture_output=True).returncode == 0


def _patch_reverse_works(tree: Path, patch: Path, strip: int) -> bool:
    argv = [
        "git", "-C", str(tree), "apply", "--reverse", "--check",
        "-p%d" % strip, str(patch),
    ]
    if subprocess.run(argv, capture_output=True).returncode == 0:
        return True
    argv = [
        "patch", "-d", str(tree), "--dry-run", "--reverse", "--forward",
        "--batch", "-p%d" % strip, "-i", str(patch),
    ]
    return subprocess.run(argv, capture_output=True).returncode == 0


def _check_or_apply_patches(
    tree: Path, patches: list[tuple[str, Path]], apply: bool
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    dropped: list[str] = []
    for name, path in patches:
        if not path.is_file():
            errors.append("%s missing" % name)
            continue
        if _patch_is_empty(path):
            continue
        strip = None
        for cand in (1, 0, 2):
            if _patch_strip_works(tree, path, cand):
                strip = cand
                break
        if strip is None:
            already = False
            for cand in (1, 0, 2):
                if _patch_reverse_works(tree, path, cand):
                    already = True
                    break
            if already:
                dropped.append(name)
                continue
            errors.append("%s does not apply" % name)
            continue
        if apply and not _apply_patch(tree, path, strip):
            errors.append("%s check passed but apply failed" % name)
    return errors, dropped


def _spec_drop_patches(text: str, names: list[str]) -> str:
    want = set(names)
    if not want:
        return text
    nums: list[str] = []

    def repl(match: re.Match[str]) -> str:
        raw = match.group("val").strip().split()[0]
        fname = Path(raw).name
        if fname in want:
            nums.append(match.group("num") or "0")
            return ""
        return match.group(0)

    out = _RE_SPEC_PATCH.sub(repl, text)
    for num in nums:
        n = num or "0"
        out = re.sub(r"(?im)^%%patch%s\b.*\n" % re.escape(n), "", out)
        out = re.sub(r"(?im)^%%patch\s+-P\s*%s\b.*\n" % re.escape(n), "", out)
    return out


_KERNEL_CFG_HOST_KEY = re.compile(
    r"^CONFIG_(?:"
    r"CC_|GCC_|CLANG_|AS_|LD_|LLD_|RUSTC_|PAHOLE_|"
    r"CC_VERSION_TEXT|CC_IS_|AS_IS_|LD_IS_|AS_VERSION|LD_VERSION|"
    r"LLD_VERSION|CLANG_VERSION|GCC_VERSION|TOOLS_SUPPORT_"
    r")"
)


def _kernel_cfg_symbol(line: str) -> str | None:
    s = (line or "").strip()
    if s.startswith("# CONFIG_") and " is not set" in s:
        return s[2:].split(" ", 1)[0]
    if s.startswith("CONFIG_") and "=" in s:
        return s.split("=", 1)[0]
    return None


def _kernel_cfg_parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        sym = _kernel_cfg_symbol(line)
        if sym:
            out[sym] = line.rstrip("\n")
    return out


def _kernel_cfg_insert_sorted(text: str, new_lines: list[str]) -> tuple[str, list[str]]:
    existing = _kernel_cfg_parse(text)
    added: list[str] = []
    pending: list[tuple[str, str]] = []
    for line in new_lines:
        sym = _kernel_cfg_symbol(line)
        if not sym or sym in existing:
            continue
        if _KERNEL_CFG_HOST_KEY.match(sym):
            continue
        pending.append((sym, line.rstrip("\n")))
        existing[sym] = line.rstrip("\n")
        added.append(sym)
    if not pending:
        return text, []
    lines = text.splitlines(keepends=True)
    if not lines:
        body = "\n".join(line for _, line in sorted(pending)) + "\n"
        return body, added
    # keep arch header line, insert the rest alphabetically by CONFIG name
    header = lines[0]
    rest = lines[1:]
    merged = rest[:]
    for sym, line in sorted(pending, key=lambda item: item[0]):
        entry = line + ("\n" if not line.endswith("\n") else "")
        insert_at = len(merged)
        for idx, cur in enumerate(merged):
            cur_sym = _kernel_cfg_symbol(cur)
            if cur_sym and cur_sym > sym:
                insert_at = idx
                break
        merged.insert(insert_at, entry)
    return header + "".join(merged), added


def _kernel_cfg_arch(cfg_path: Path) -> str | None:
    try:
        first = cfg_path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    first = first.strip()
    if first.startswith("# ") and first[2:].strip() and first[2:].strip() != "EMPTY":
        return first[2:].strip()
    return None


def _kernel_cfg_listnew_defaults(tree: Path, arch: str, cfg_path: Path) -> tuple[list[str], str | None]:
    work = tree / ".orbscli-kconfig"
    work.mkdir(parents=True, exist_ok=True)
    cfg_tmp = work / (cfg_path.name + ".work")
    shutil.copy2(cfg_path, cfg_tmp)
    cross = "scripts/dummy-tools/"
    env = {**os.environ, "KCONFIG_CONFIG": str(cfg_tmp)}
    list_proc = subprocess.run(
        [
            "make",
            "ARCH=%s" % arch,
            "CROSS_COMPILE=%s" % cross,
            "KCONFIG_CONFIG=%s" % cfg_tmp,
            "listnewconfig",
        ],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    blob = "%s\n%s" % (list_proc.stdout or "", list_proc.stderr or "")
    new_syms: list[str] = []
    seen: set[str] = set()
    for line in blob.splitlines():
        sym = _kernel_cfg_symbol(line)
        if not sym or sym in seen or _KERNEL_CFG_HOST_KEY.match(sym):
            continue
        seen.add(sym)
        new_syms.append(sym)
    if not new_syms:
        cfg_tmp.unlink(missing_ok=True)
        return [], None
    old_proc = subprocess.run(
        [
            "make",
            "ARCH=%s" % arch,
            "CROSS_COMPILE=%s" % cross,
            "KCONFIG_CONFIG=%s" % cfg_tmp,
            "olddefconfig",
        ],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    if old_proc.returncode != 0:
        return [], "olddefconfig failed for %s" % cfg_path.name
    after = _kernel_cfg_parse(cfg_tmp.read_text(encoding="utf-8", errors="replace"))
    cfg_tmp.unlink(missing_ok=True)
    lines = [after[sym] for sym in new_syms if sym in after]
    return lines, None


def _kernel_refresh_configs(spec_dir: Path, tree: Path, pkg: str) -> str | None:
    if not (tree / "MAINTAINERS").is_file() or not (tree / "drivers").is_dir():
        return "unpacked source is not a kernel tree"
    # RH Source configs are sorted sparse files. never write full olddefconfig dumps back.
    configs = sorted(
        p
        for p in spec_dir.glob("%s-*.config" % pkg)
        if p.name.endswith(("-fedora.config", "-rhel.config"))
    )
    if not configs:
        return "no %s-*-{fedora,rhel}.config files to refresh" % pkg
    added_total = 0
    for cfg in configs:
        arch = _kernel_cfg_arch(cfg)
        if not arch:
            continue
        try:
            new_lines, err = _kernel_cfg_listnew_defaults(tree, arch, cfg)
        except subprocess.TimeoutExpired:
            return "kconfig timed out on %s" % cfg.name
        if err:
            return err
        if not new_lines:
            continue
        text = cfg.read_text(encoding="utf-8", errors="replace")
        merged, added = _kernel_cfg_insert_sorted(text, new_lines)
        if added:
            cfg.write_text(merged, encoding="utf-8")
            added_total += len(added)
            click.echo(
                "%s  +%d new kconfig symbol(s) in %s" % (pkg, len(added), cfg.name)
            )
    if added_total == 0:
        click.echo("%s  no new kconfig symbols" % pkg)
    return None


def _source_url_basename(url: str) -> str:
    path = (url or "").strip().split("?", 1)[0].rstrip("/")
    if not path:
        return ""
    return path.rsplit("/", 1)[-1]


def _source_basename_compatible(want: str, got: str) -> bool:
    # same file family, version digits in the name may differ
    w = (want or "").strip().lower()
    g = (got or "").strip().lower()
    if not w or not g:
        return False
    if w == g:
        return True
    wr = re.sub(r"\d+", "N", w)
    gr = re.sub(r"\d+", "N", g)
    return wr == gr and bool(wr)


def _macroize_url(raw_url: str, macros: dict[str, str], version: str | None = None) -> str:
    url = (raw_url or "").strip()
    if version and "%" not in version and version in url:
        url = url.replace(version, "%{version}")
    for macro_name, value in sorted(macros.items(), key=lambda item: -len(item[1] or "")):
        if macro_name in ("version", "name") or len(value or "") < 3:
            continue
        if "%" in value or value not in url:
            continue
        url = url.replace(value, "%{" + macro_name + "}")
    return url


def _set_source_hash_guard(text: str, digest: str, number: str = "0") -> str:
    """Keep exactly one ``sourceN_hash`` guard line for the fetched digest."""
    macro = "source" + str(number or "0") + "_hash"
    preamble, changelog = _split_spec_changelog(text)
    body, _ = re.subn(
        r"(?im)^[ \t]*%(?:global|define)\s+" + re.escape(macro) + r"(?:[ \t]+\S+)?.*\n?",
        "",
        preamble,
    )
    body = body.lstrip("\n")
    guard = "%%global %s %s\n" % (macro, digest)
    return (guard + "\n" + body if body else guard) + changelog


def find_pkg_specs(root: Path, pkg: str) -> list[Path]:
    root = root.expanduser().resolve()
    direct = root / pkg / f"{pkg}.spec"
    if direct.is_file():
        return [direct]
    hits: list[Path] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        p = child / pkg / f"{pkg}.spec"
        if p.is_file():
            hits.append(p)
    return hits


_RE_SPEC_NAME = re.compile(r"(?im)^Name\s*:\s*(\S+)")
_RE_SPEC_BR = re.compile(r"(?im)^BuildRequires(?:\((?:post|pre|postun|preun)\))?\s*:\s*(.+)$")
_RE_SPEC_PROVIDES = re.compile(r"(?im)^Provides\s*:\s*(.+)$")
_RE_SPEC_PACKAGE = re.compile(r"(?im)^%package(?:\s+-n\s+(\S+)|\s+(\S+))\s*$")
_RE_SPEC_FILES = re.compile(r"(?im)^%files(?:\s+-n\s+(\S+)|\s+(\S+))?\s*.*$")
_RE_SPEC_STOP = re.compile(r"(?im)^%(?:prep|build|install|check|clean|changelog)\b")
_RE_DEP_ATOM = re.compile(
    r"/(?:usr|bin|etc|opt)[A-Za-z0-9._+~/-]*|"
    r"(?:pkgconfig|cmake|python3(?:\.\d+)?dist|perl|tex|font|rubygem|bundled)\([^)]+\)|"
    r"[A-Za-z0-9_][A-Za-z0-9._+~:-]*\([^)]+\)|"
    r"[A-Za-z0-9_][A-Za-z0-9._+~:-]*"
)
_RE_PC_FILE = re.compile(r"(?im)(?:^|/)([A-Za-z0-9._+-]+)\.pc\s*$")
_RE_CMAKE_DIR = re.compile(
    r"(?i)(?:^|/)cmake/([A-Za-z0-9._+-]+)(?:/?(?:\*\.cmake)?/?)\s*$"
)
_RE_CMAKE_FILE = re.compile(r"(?i)(?:^|/)([A-Za-z0-9._+-]+)Config\.cmake\s*$")
_RE_TEX_FILE = re.compile(r"(?i)(?:^|/)([^/]+\.(?:sty|cls|tfm|def|fd|cfg|clo))\s*$")
_RE_BINDIR_FILE = re.compile(r"%\{_bindir\}/([A-Za-z0-9._+-]+)")
_RE_SCRIPT_BINDIR = re.compile(
    r"(?:%\{_?bindir\}|/usr/bin|/bin)/([A-Za-z0-9._+-]+)"
)
_RE_CMAKE_EXE_FLAG = re.compile(
    r"(?i)-D[A-Za-z0-9_]*(?:EXECUTABLE|EXE_NAME|BIN_NAME|BINARY_NAME|PROGRAM_NAME)\s*=\s*([A-Za-z0-9._+-]+)"
)
_RE_ADD_EXECUTABLE = re.compile(
    r"(?i)(?:add_executable|executable)\s*\(\s*['\"]?([A-Za-z0-9._+-]+)"
)
_CHAIN_NOISE = {
    "version", "release", "epoch", "dist", "name", "isa",
    "with", "without", "if", "endif", "else",
    "widgets", "libs", "core", "common", "doc", "devel",
    "utils", "tools", "data", "files", "headers", "static",
    "usr", "bin", "major", "minor",
}
_MIN_REPO_RPMS = 200


def _note_bin_provide(provides: set[str], name: str) -> None:
    if not name or "*" in name or "%" in name:
        return
    provides.add("/usr/bin/" + name)
    provides.add("/bin/" + name)
    provides.add(name)


def _spec_note_output_bins(line: str, provides: set[str]) -> None:
    s = line.split("#", 1)[0]
    for m in _RE_SCRIPT_BINDIR.finditer(s):
        _note_bin_provide(provides, m.group(1))
    for m in _RE_CMAKE_EXE_FLAG.finditer(s):
        _note_bin_provide(provides, m.group(1))
    for m in _RE_ADD_EXECUTABLE.finditer(s):
        _note_bin_provide(provides, m.group(1))


def _yum_file_atoms(path: str, pkg: str, provides: dict[str, set[str]]) -> None:
    if not path or path.endswith("/"):
        return
    provides.setdefault(path, set()).add(pkg)
    if path.startswith(("/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/", "/usr/libexec/")):
        base = path.rsplit("/", 1)[-1]
        if base and "*" not in base:
            provides.setdefault(base, set()).add(pkg)


def _spec_if_truth(cond: str) -> bool | None:
    raw = re.sub(r"#.*", "", cond).strip()
    if not raw:
        return True
    compact = re.sub(r"\s+", " ", raw)
    if re.fullmatch(r"0+", compact):
        return False
    if compact in {"1", "true"}:
        return True
    if re.search(r"0%\{\??fedora\}|%\{defined fedora\}", compact):
        return False
    if re.search(r"0%\{\??rhel\}", compact):
        return False
    return None


def _spec_dep_atoms(value: str) -> list[str]:
    line = value.split("#", 1)[0]
    line = re.sub(r"%\{[^}]+\}", " ", line)
    line = re.sub(r"(>=|<=|==|=|<|>)\s*\S+", " ", line)
    out: list[str] = []
    for match in _RE_DEP_ATOM.finditer(line):
        token = match.group(0).strip()
        if not token or token.lower() in _CHAIN_NOISE:
            continue
        if token.startswith("%") or token.startswith("bundled("):
            continue
        if re.fullmatch(r"[\d._~^]+", token):
            continue
        out.append(token)
    return out


def _hackage_nv(token: str) -> str | None:
    m = re.fullmatch(r"(.+)-(\d[\d.]*)$", token.strip())
    if not m:
        return None
    return m.group(1)


def _ghc_subpkg_rpms(hackage: str) -> list[str]:
    name = _hackage_nv(hackage) or hackage.strip()
    if not name:
        return []
    base = "ghc-" + name
    return [base, base + "-devel", base + "-prof", base + "-doc"]


def _ghc_lib_subpackage_hackage(line: str, macros: dict[str, str]) -> str | None:
    if not re.match(r"%ghc_lib_subpackage\b", line):
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    token = parts[-1]
    token = _expand_source_macros(token, macros)
    if token.startswith("%{") and token.endswith("}"):
        token = macros.get(token[2:-1], "")
    token = re.sub(r"-%\{[^}]+\}$", "", token)
    if not token or "%" in token:
        return None
    return token


def _spec_expand_name_token(token: str, pkg_name: str) -> str:
    return token.replace("%{name}", pkg_name).replace("%{NAME}", pkg_name)


class _SpecMeta:
    __slots__ = ("name", "path", "provides", "needs")

    def __init__(self, name: str, path: Path, provides: set[str], needs: set[str]) -> None:
        self.name = name
        self.path = path
        self.provides = provides
        self.needs = needs


_SPEC_META_CACHE: dict[str, _SpecMeta | None] = {}


def _spec_meta(path: Path) -> _SpecMeta | None:
    key = str(path)
    cached = _SPEC_META_CACHE.get(key)
    if cached is not None or key in _SPEC_META_CACHE:
        return cached
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _SPEC_META_CACHE[key] = None
        return None
    preamble, _ = _split_spec_changelog(text)
    match = _RE_SPEC_NAME.search(preamble)
    raw_name = match.group(1).strip() if match else path.stem
    pkg = raw_name if "%" not in raw_name else path.parent.name
    macros: dict[str, str] = {"name": pkg}
    for define in _RE_SPEC_DEFINE.finditer(preamble):
        val = (define.group("val") or "").strip()
        if val and "%" not in val:
            macros[define.group("name")] = val
    provides: set[str] = {pkg, path.parent.name, path.stem}
    needs: set[str] = set()
    active = [True]
    in_files = False
    in_script = False
    for raw in preamble.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("%if"):
            cond = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else "1"
            truth = _spec_if_truth(cond)
            active.append(active[-1] if truth is None else (active[-1] and truth))
            continue
        if line.startswith("%elif"):
            if len(active) < 2:
                continue
            cond = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else "1"
            truth = _spec_if_truth(cond)
            parent = active[-2]
            active[-1] = parent if truth is None else (parent and truth)
            continue
        if line.startswith("%else"):
            if len(active) >= 2:
                active[-1] = active[-2] and not active[-1]
            continue
        if line.startswith("%endif"):
            if len(active) > 1:
                active.pop()
            continue
        if _RE_SPEC_STOP.match(line):
            in_script = True
            in_files = False
            continue
        files = _RE_SPEC_FILES.match(line)
        if files:
            in_script = False
            in_files = True
            continue
        pkg_m = _RE_SPEC_PACKAGE.match(line)
        if pkg_m:
            in_script = False
            in_files = False
            if not active[-1]:
                continue
            extra = pkg_m.group(1) or pkg_m.group(2)
            extra = extra.strip()
            extra = extra.replace("%{python3_pkgversion}", "3")
            extra = extra.replace("%{?python3_pkgversion}", "3")
            extra = extra.replace("%{python_pkgversion}", "3")
            extra = _spec_expand_name_token(extra, pkg)
            extra = _expand_source_macros(extra, macros)
            if "%" not in extra:
                if pkg_m.group(1):
                    provides.add(extra)
                else:
                    provides.add(pkg + "-" + extra)
            continue
        hackage = _ghc_lib_subpackage_hackage(line, macros)
        if hackage:
            if not active[-1]:
                continue
            for rpm_name in _ghc_subpkg_rpms(hackage):
                provides.add(rpm_name)
            continue
        if re.match(r"^%(?:description|pre|preun|post|postun|trigger|verifyscript|transfiletrigger)\b", line):
            in_files = False
            in_script = False
            continue
        if not active[-1]:
            continue
        mvn_m = re.match(r"^%mvn_(?:file|artifact)\s+(\S+)", line)
        if mvn_m:
            coord = mvn_m.group(1)
            if (
                ":" in coord
                and "%" not in coord
                and "*" not in coord
                and "{" not in coord
                and not coord.endswith(".xml")
            ):
                provides.add("mvn(" + coord + ")")
            continue
        if in_script:
            _spec_note_output_bins(line, provides)
            continue
        if in_files:
            expanded = _expand_source_macros(line, macros)
            pc = _RE_PC_FILE.search(expanded)
            if pc:
                provides.add("pkgconfig(" + pc.group(1) + ")")
            cmake_dir = _RE_CMAKE_DIR.search(expanded)
            if cmake_dir:
                provides.add("cmake(" + cmake_dir.group(1) + ")")
            cmake_file = _RE_CMAKE_FILE.search(expanded)
            if cmake_file:
                provides.add("cmake(" + cmake_file.group(1) + ")")
            tex = _RE_TEX_FILE.search(expanded)
            if tex:
                provides.add("tex(" + tex.group(1) + ")")
            bindir = _RE_BINDIR_FILE.search(line)
            if bindir:
                _note_bin_provide(provides, bindir.group(1))
            _spec_note_output_bins(expanded, provides)
            if expanded.startswith(("/usr/bin/", "/bin/", "/usr/sbin/")):
                provides.add(expanded.split()[0])
            continue
        prov_m = _RE_SPEC_PROVIDES.match(line)
        if prov_m:
            raw_prov = _expand_source_macros(_spec_expand_name_token(prov_m.group(1), pkg), macros)
            for atom in _spec_dep_atoms(raw_prov):
                if "%" not in atom:
                    provides.add(atom)
            continue
        br = _RE_SPEC_BR.match(line)
        if br:
            for atom in _spec_dep_atoms(br.group(1)):
                atom = _expand_source_macros(_spec_expand_name_token(atom, pkg), macros)
                if "%" in atom:
                    continue
                needs.add(atom)
    for key in ("pypi_name", "pkgname", "srcname", "modname"):
        val = macros.get(key)
        if val and "%" not in val:
            provides.add("python3dist(" + val.lower().replace("-", "_") + ")")
            provides.add("python3-" + val.replace("_", "-"))
    for item in list(provides):
        if item.startswith("python3-") and "(" not in item:
            provides.add("python3dist(" + item[len("python3-") :].replace("-", "_") + ")")
    meta = _SpecMeta(pkg, path, provides, needs)
    _SPEC_META_CACHE[key] = meta
    return meta


def _spec_package_name(path: Path) -> str | None:
    meta = _spec_meta(path)
    return meta.name if meta else None


def _spec_buildrequires(path: Path) -> set[str]:
    meta = _spec_meta(path)
    return set(meta.needs) if meta else set()


def _git_changed_spec_paths(repo_root: Path, spec_root: Path, pushed: bool) -> list[Path]:
    import subprocess

    if pushed:
        commands = [("git", "diff", "--name-only", "origin/HEAD...HEAD", "--")]
    else:
        commands = [("git", "status", "--porcelain", "--")]
    try:
        proc = subprocess.run(
            commands[0], cwd=repo_root, check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    names: list[str] = []
    for line in proc.stdout.splitlines():
        raw = line[3:] if not pushed and len(line) >= 3 else line
        if pushed:
            raw = line.strip()
        if raw.endswith(".spec"):
            names.append(raw)
    out: list[Path] = []
    for name in names:
        path = (repo_root / name).resolve()
        try:
            path.relative_to(spec_root.resolve())
        except ValueError:
            continue
        if path.is_file():
            out.append(path)
    return sorted(set(out))


def _chain_seed_names(changed: list[Path]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for path in changed:
        name = path.parent.name
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


_PROVIDE_INDEX_CACHE: dict[str, dict[str, str]] = {}


def _provide_index(spec_root: Path) -> dict[str, str]:
    key = str(spec_root)
    hit = _PROVIDE_INDEX_CACHE.get(key)
    if hit is not None:
        return hit
    out: dict[str, str] = {}

    def add(atom: str, src: str) -> None:
        if atom and "%" not in atom:
            out.setdefault(atom.lower(), src)

    try:
        kids = [p for p in spec_root.iterdir() if (p / (p.name + ".spec")).is_file()]
    except OSError:
        _PROVIDE_INDEX_CACHE[key] = out
        return out
    for child in kids:
        src = child.name
        add(src, src)
        add(src + "-devel", src)
        add(src + "-prof", src)
    total = len(kids)
    click.echo("index %d specs" % total, err=True)

    def _meta_one(child: Path) -> tuple[str, _SpecMeta | None]:
        return child.name, _spec_meta(child / (child.name + ".spec"))

    done = 0
    workers = min(8, _host_workers("cpu"))
    for src, meta in _iter_bounded(workers, _meta_one, kids, inflight=workers):
        done += 1
        if done % 500 == 0 or done == total:
            _status_line("index %d/%d" % (done, total))
        if meta is None:
            continue
        add(meta.name, src)
        add(meta.name + "-devel", src)
        for atom in meta.provides:
            add(atom, src)
            if atom.endswith("-devel"):
                add(atom[: -len("-devel")], src)
    _status_nl()
    _PROVIDE_INDEX_CACHE[key] = out
    return out


def _chain_provider_aliases(atom: str) -> list[str]:
    aliases = [atom]
    wrapped = re.fullmatch(
        r"(pkgconfig|cmake|python3(?:\.\d+)?dist|perl|tex|font|rubygem|crate)\(([^)]+)\)",
        atom,
    )
    if not wrapped:
        return aliases
    kind = wrapped.group(1)
    inner = wrapped.group(2)
    if kind == "crate":
        crate = inner.split("/", 1)[0]
        aliases.append("rust-" + crate.replace("_", "-"))
        aliases.append("rust-" + crate)
        return aliases
    if inner not in aliases:
        aliases.append(inner)
    if inner.endswith("-devel"):
        aliases.append(inner[: -len("-devel")])
    if inner.endswith("-prof"):
        aliases.append(inner[: -len("-prof")])
    return aliases


_RPM_ARCH_FLAVOR = re.compile(
    r"^(.+)\((x86-64|aarch-64|ppc64le|s390x|i686|armv7hl|noarch)\)$"
)


def _unflavor_rpm_atom(atom: str) -> str:
    m = _RPM_ARCH_FLAVOR.fullmatch(atom)
    return m.group(1) if m else atom


def _repo_virtual_keys(atom: str) -> list[str]:
    keys: list[str] = [atom]
    bare = _unflavor_rpm_atom(atom)
    if bare != atom:
        keys.append(bare)
    crate = re.fullmatch(r"crate\(([^)]+)\)", atom)
    if crate:
        inner = crate.group(1)
        base = inner.split("/", 1)[0]
        dashed = base.replace("_", "-")
        keys.extend(
            [
                "crate(" + base + ")",
                "crate(" + base + "/default)",
                "crate(" + inner + ")",
                "crate(" + dashed + ")",
                "crate(" + dashed + "/default)",
            ]
        )
    py = re.fullmatch(r"python3(?:\.\d+)?dist\(([^)]+)\)", atom)
    if py:
        inner = re.sub(r"\[.*\]", "", py.group(1))
        dashed = inner.replace("_", "-")
        und = inner.replace("-", "_")
        keys.extend(
            [
                "python3dist(" + inner + ")",
                "python3dist(" + dashed + ")",
                "python3dist(" + und + ")",
                "python3dist(" + inner.replace(".", "_") + ")",
                "python3dist(" + dashed.replace(".", "_") + ")",
            ]
        )
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _repo_has_atom(atom: str, present: set[str]) -> bool:
    atom = _provide_atom(atom) or atom
    if atom in present:
        return True
    if atom.endswith("()"):
        base = atom[:-2]
        return (base + "()(64bit)") in present or (base + "()(32bit)") in present
    for key in _repo_virtual_keys(atom):
        if key in present or key.lower() in present:
            return True
    return False


def _lookup_keys(atom: str) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()

    def push(item: str) -> None:
        if item and item not in seen:
            seen.add(item)
            keys.append(item)

    push(atom)
    for alias in _chain_provider_aliases(atom):
        push(alias)
    wrapped = re.fullmatch(
        r"(pkgconfig|cmake|python3(?:\.\d+)?dist|perl|tex|font|rubygem|crate)\(([^)]+)\)",
        atom,
    )
    if wrapped and wrapped.group(1).startswith("python3"):
        inner = re.sub(r"\[.*\]", "", wrapped.group(2))
        push("python3dist(" + inner + ")")
        dashed = inner.replace("_", "-")
        underscored = inner.replace("-", "_")
        push("python3dist(" + dashed + ")")
        push("python3dist(" + underscored + ")")
        push(dashed)
        push(underscored)
        push("python3-" + dashed)
        push("python-" + dashed)
        push("python3-" + underscored)
        push("python-" + underscored)
    if wrapped and wrapped.group(1) == "tex":
        stem = wrapped.group(2).rsplit(".", 1)[0]
        push("texlive-" + stem)
    if wrapped and wrapped.group(1) == "crate":
        crate = wrapped.group(2).split("/", 1)[0].replace("_", "-")
        push("rust-" + crate)
        push("rust-" + wrapped.group(2).split("/", 1)[0])
    ghc = _ghc_stem_provider(atom)
    if ghc:
        push(ghc)
        push(ghc + "-devel")
        push(ghc + "-prof")
    return keys


def _rpmvercmp(a: str, b: str) -> int:
    if a == b:
        return 0
    i = j = 0
    la, lb = len(a), len(b)
    while i < la or j < lb:
        while i < la and not a[i].isalnum() and a[i] not in "~^":
            i += 1
        while j < lb and not b[j].isalnum() and b[j] not in "~^":
            j += 1
        ta = i < la and a[i] == "~"
        tb = j < lb and b[j] == "~"
        if ta or tb:
            if not ta:
                return 1
            if not tb:
                return -1
            i += 1
            j += 1
            continue
        ca = i < la and a[i] == "^"
        cb = j < lb and b[j] == "^"
        if ca or cb:
            if i >= la:
                return -1
            if j >= lb:
                return 1
            if not ca:
                return 1
            if not cb:
                return -1
            i += 1
            j += 1
            continue
        if i >= la or j >= lb:
            break
        if a[i].isdigit():
            e = i
            while e < la and a[e].isdigit():
                e += 1
            sa, a_num = a[i:e], True
            i = e
        else:
            e = i
            while e < la and a[e].isalpha():
                e += 1
            sa, a_num = a[i:e], False
            i = e
        if j < lb and b[j].isdigit():
            e = j
            while e < lb and b[e].isdigit():
                e += 1
            sb, b_num = b[j:e], True
            j = e
        else:
            e = j
            while e < lb and b[e].isalpha():
                e += 1
            sb, b_num = b[j:e], False
            j = e
        if b_num and not a_num:
            return 1
        if a_num and not b_num:
            return -1
        if a_num:
            sa2 = sa.lstrip("0") or "0"
            sb2 = sb.lstrip("0") or "0"
            if len(sa2) != len(sb2):
                return 1 if len(sa2) > len(sb2) else -1
            if sa2 != sb2:
                return 1 if sa2 > sb2 else -1
        elif sa != sb:
            return 1 if sa > sb else -1
    if i < la:
        return 1
    if j < lb:
        return -1
    return 0


def _evr_split(evr: str) -> tuple[int, str, str]:
    s = re.sub(r"~+$", "", (evr or "").strip())
    epoch = 0
    if ":" in s:
        head, tail = s.split(":", 1)
        if head.isdigit():
            epoch = int(head)
            s = tail
    ver, _, rel = s.partition("-")
    return epoch, ver, rel


def _evr_compare(a: tuple[int, str, str], b: tuple[int, str, str]) -> int:
    if a[0] != b[0]:
        return 1 if a[0] > b[0] else -1
    c = _rpmvercmp(a[1], b[1])
    if c:
        return c
    return _rpmvercmp(a[2], b[2])


def _constraint_ok(op: str, want: str, cand: tuple[int, str, str]) -> bool:
    c = _evr_compare(cand, _evr_split(want))
    if op in ("=", "=="):
        return c == 0
    if op == "<":
        return c < 0
    if op == "<=":
        return c <= 0
    if op == ">":
        return c > 0
    return c >= 0


_RE_DEP_CONSTRAINT = re.compile(r"([A-Za-z0-9_][A-Za-z0-9._+~-]*(?:\([^)]+\))?)\s*(>=|<=|==|=|<|>)\s*(\S+)")


def _br_all_constraints(raw: str) -> list[tuple[str, str, str]]:
    s = raw.strip()
    body = s[1:-1] if s.startswith("(") and s.endswith(")") else s
    body = re.split(r"\s+if\s+", body, maxsplit=1, flags=re.I)[0]
    return [(m.group(1), m.group(2), m.group(3)) for m in _RE_DEP_CONSTRAINT.finditer(body)]


def _br_constraints(raw: str) -> list[tuple[str, str, str]]:
    s = raw.strip()
    if s.startswith("("):
        return _br_all_constraints(raw)
    body = re.split(r"\s+if\s+", s, maxsplit=1)[0]
    return [(m.group(1), m.group(2), m.group(3)) for m in _RE_DEP_CONSTRAINT.finditer(body)]


def _evr_matches_constraints(
    evr: tuple[int, str, str],
    wants: list[tuple[str, str]],
) -> bool:
    return all(_constraint_ok(op, ver, evr) for op, ver in wants)


def _atom_repo_ok(
    atom: str,
    wants: list[tuple[str, str]],
    caps: set[str],
    detail: _RepoDetail | None,
) -> bool:
    if not _repo_has_atom(atom, caps):
        return False
    if not wants or detail is None:
        return True
    evrs: set[tuple[int, str, str]] = set()
    for key in _repo_virtual_keys(atom):
        evrs |= set(detail.provides_evr.get(key) or ())
    crate = re.fullmatch(r"crate\(([^)]+)\)", atom)
    if crate:
        inner = crate.group(1)
        base = inner.split("/", 1)[0]
        for key in ("crate(" + base + ")", "crate(" + base + "/default)"):
            evrs |= set(detail.provides_evr.get(key) or ())
    if not evrs:
        return not wants
    return any(_evr_matches_constraints(evr, wants) for evr in evrs)


def _pkg_requires_met(
    pkg: str,
    caps: set[str],
    detail: "_RepoDetail",
    depth: int = 0,
) -> bool:
    """True when Requires of pkg are present and one extra hop is installable.

    ghc-tasty-devel Requires ghc-tasty, which needs libHS*.so. mock walks that.
    do not recurse the whole yum graph or generate-chain hangs on spectacle.
    """
    hit = detail._req_met.get((pkg, depth))
    if hit is not None:
        return hit
    or_groups: dict[str, list[str]] = {}
    for req, cond, grp in detail.requires.get(pkg, ()):
        if req.startswith(("/", "rpmlib(", "config(", "rtld(")):
            continue
        if cond is not None:
            try:
                if not _rich_cond_holds(cond, detail, {pkg}):
                    continue
            except Exception:
                pass
        if grp is not None:
            or_groups.setdefault(str(grp), []).append(req)
            continue
        if not _repo_has_atom(req, caps):
            detail._req_met[(pkg, depth)] = False
            return False
        if depth >= 1:
            continue
        if ".so" in req and "libHS" not in req:
            continue
        providers = _yum_provider_rpms(req, detail)
        if providers and not any(_pkg_requires_met(p, caps, detail, depth + 1) for p in providers):
            detail._req_met[(pkg, depth)] = False
            return False
    for alts in or_groups.values():
        if not alts:
            continue
        ok = False
        for atom in alts:
            if not _repo_has_atom(atom, caps):
                continue
            if depth >= 1:
                ok = True
                break
            providers = _yum_provider_rpms(atom, detail)
            if not providers or any(_pkg_requires_met(p, caps, detail, depth + 1) for p in providers):
                ok = True
                break
        if not ok:
            detail._req_met[(pkg, depth)] = False
            return False
    detail._req_met[(pkg, depth)] = True
    return True


def _needs_nested_install_check(atom: str) -> bool:
    a = atom.lower()
    return (
        a.startswith("ghc-")
        or a.startswith("ghc-devel(")
        or "libhs" in a
        or a.startswith("cmake(")
        or a.startswith("pkgconfig(")
        or a.startswith("python3dist(")
        or a.startswith("python3.")
    )


def _rpm_req(atom: str, wants: list[tuple[str, str]]) -> str:
    if not wants:
        return atom
    parts = ["%s %s %s" % (atom, op, ver) for op, ver in wants if op and ver]
    if not parts:
        return atom
    if len(parts) == 1:
        return parts[0]
    return "(" + " with ".join(parts) + ")"


def _atom_br_ok(
    atom: str,
    wants: list[tuple[str, str]],
    caps: set[str],
    detail: "_RepoDetail | None",
) -> bool:
    if _needs_nested_install_check(atom):
        return _atom_installable(atom, wants, caps, detail)
    return _atom_repo_ok(atom, wants, caps, detail)


def _atom_installable(
    atom: str,
    wants: list[tuple[str, str]],
    caps: set[str],
    detail: "_RepoDetail | None",
) -> bool:
    """Like _atom_repo_ok, but false when every provider RPM has missing Requires."""
    if not _atom_repo_ok(atom, wants, caps, detail):
        return False
    if detail is None:
        return True
    ikey = (atom, tuple(wants))
    cached = detail._inst.get(ikey)
    if cached is not None:
        return cached
    pkgs = _yum_provider_rpms(atom, detail)
    if not pkgs:
        pkgs = set(detail.provides.get(atom) or ()) | set(detail.provides.get(atom.lower()) or ())
        if len(pkgs) > 8:
            detail._inst[ikey] = True
            return True
    if not pkgs:
        detail._inst[ikey] = True
        return True
    ok = any(_pkg_requires_met(pkg, caps, detail) for pkg in pkgs)
    detail._inst[ikey] = ok
    return ok


def _spec_declares_devel(spec_root: Path | None, name: str) -> bool:
    if spec_root is None:
        return True
    path = spec_root / name / (name + ".spec")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return bool(
        re.search(
            r"(?im)^%package(?:\s+devel\b|\s+-n\s+(?:%\{\??name\}-devel|"
            + re.escape(name)
            + r"-devel)\b)",
            text,
        )
    )


def _spec_presence_atoms(name: str, spec_root: Path | None = None) -> list[str]:
    n = name
    atoms = [n, n + "-devel"]
    if n.startswith("rust-"):
        crate = n[5:]
        und = crate.replace("-", "_")
        atoms += [
            "crate(" + crate + ")",
            "crate(" + crate + "/default)",
            "crate(" + und + ")",
            "crate(" + und + "/default)",
            n + "-devel",
        ]
    elif n.startswith("python-"):
        stem = n[7:]
        dashed = stem.replace("_", "-")
        und = stem.replace("-", "_")
        atoms += [
            "python3-" + dashed,
            "python3-" + stem,
            "python3dist(" + dashed + ")",
            "python3dist(" + und + ")",
        ]
    elif n.startswith("ghc-"):
        atoms += [n + "-prof", "ghc-devel(" + n[4:] + ")"]
    if spec_root is not None:
        meta = _spec_meta(spec_root / n / (n + ".spec"))
        if meta is not None:
            for prov in meta.provides:
                if prov and "%" not in prov:
                    atoms.append(prov)
    extra: list[str] = []
    for item in atoms:
        if item.startswith("python3-") and "(" not in item:
            stem = item[8:]
            extra.append("python3dist(" + stem + ")")
            extra.append("python3dist(" + stem.replace("-", "_") + ")")
    atoms.extend(extra)
    return list(dict.fromkeys(atoms))


_BOOT_SONAME = re.compile(
    r"^(ld-linux|libc\.so|libm\.so|libpthread\.so|libdl\.so|librt\.so|"
    r"libgcc_s\.so|libstdc\+\+\.so|libgomp\.so)"
)


def _skip_runtime_req(req: str) -> bool:
    if not req or req.startswith(("/", "rpmlib(", "config(", "rtld(")):
        return True
    base = req.split("(")[0]
    return bool(_BOOT_SONAME.match(base))


def _rpm_runtime_ok(pkg: str, caps: set[str], detail: "_RepoDetail | None") -> bool:
    if detail is None:
        return True
    or_groups: dict[str, list[str]] = {}
    for req, cond, grp in detail.requires.get(pkg, ()):
        if _skip_runtime_req(req):
            continue
        if cond is not None:
            continue
        if grp is not None:
            or_groups.setdefault(str(grp), []).append(req)
            continue
        if not _repo_has_atom(req, caps):
            return False
    for alts in or_groups.values():
        if alts and not any(_repo_has_atom(a, caps) for a in alts):
            return False
    return True


def _soname_spec_guesses(atom: str) -> list[str]:
    raw = atom.split("(")[0]
    if ".so" not in raw or not raw.startswith("lib"):
        return []
    stem = raw[3 : raw.find(".so")]
    if not stem:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def push(item: str) -> None:
        low = item.lower()
        if low and low not in seen:
            seen.add(low)
            out.append(low)

    push(stem)
    push(stem.replace("_", "-"))
    parts = stem.replace("-", "_").split("_")
    for i in range(len(parts), 0, -1):
        push("-".join(parts[:i]))
        push("".join(parts[:i]))
    if stem.startswith("Qt6") and len(stem) > 3:
        rest = stem[3:]
        qt = {
            "Qml": "qt6-qtdeclarative",
            "Quick": "qt6-qtdeclarative",
            "QuickControls2": "qt6-qtquickcontrols2",
            "Gui": "qt6-qtbase",
            "Core": "qt6-qtbase",
            "Widgets": "qt6-qtbase",
            "Network": "qt6-qtbase",
            "DBus": "qt6-qtbase",
            "OpenGL": "qt6-qtbase",
            "Wayland": "qt6-qtwayland",
        }
        for key, spec in qt.items():
            if rest == key or rest.startswith(key):
                push(spec)
                break
    return out


_YUM_SRPM_CACHE: dict[int, set[str] | None] = {}


def _sourcerpm_to_name(srpm: str) -> str:
    s = (srpm or "").strip()
    if not s or s.lower() in {"(none)", "none"}:
        return ""
    if s.endswith(".src.rpm"):
        s = s[: -len(".src.rpm")]
    elif s.endswith(".nosrc.rpm"):
        s = s[: -len(".nosrc.rpm")]
    return _nevra_to_name(s)


def _yum_srpm_names(
    satisfied: set[str] | dict[str, set[str]] | None,
) -> set[str] | None:
    if satisfied is None:
        return None
    key = id(satisfied)
    if key in _YUM_SRPM_CACHE:
        return _YUM_SRPM_CACHE[key]
    acc: set[str] | None = None
    for target in _satisfied_targets(satisfied):
        detail = _detail_for_target(satisfied, target)
        if detail is not None:
            chunk = set(detail.srpms)
        else:
            chunk = set(_caps_for_target(satisfied, target) or ())
        if acc is None:
            acc = chunk
        else:
            acc &= chunk
    out = acc if acc is not None else set()
    _YUM_SRPM_CACHE[key] = out
    return out


def _spec_in_yum(
    name: str,
    satisfied: set[str] | dict[str, set[str]] | None,
    spec_root: Path | None = None,
) -> bool:
    srpms = _yum_srpm_names(satisfied)
    if not srpms:
        return False
    return name in srpms


def _repo_binary_versions(detail: "_RepoDetail", name: str) -> set[str]:
    out: set[str] = set()
    for key in (name, name.lower()):
        for evr in detail.provides_evr.get(key) or ():
            if evr[1]:
                out.add(evr[1])
    return out


def _repo_has_spec_version(
    present: set[str] | dict[str, Any] | None, name: str, spec_ver: str
) -> bool:
    if not spec_ver or not isinstance(present, dict) or not present:
        return False
    for _arch, detail in present.items():
        if not isinstance(detail, _RepoDetail):
            return False
        vers = _repo_binary_versions(detail, name)
        if spec_ver not in vers:
            return False
    return True


def _stale_distgit_seeds(
    spec_root: Path, present: set[str] | dict[str, Any] | None
) -> list[str]:
    packed = _spec_dirs_with_specs(spec_root)
    total = len(packed)
    stale: list[str] = []
    click.echo("scan %d distgit specs for repo Version mismatch" % total, err=True)
    done = 0
    for name, spec in packed:
        done += 1
        if done % 500 == 0:
            _status_line("stale-scan %d/%d" % (done, total))
        try:
            text = spec.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stale.append(name)
            continue
        found = _spec_literal_version(_split_spec_changelog(text)[0])
        ver = found[0] if found else ""
        if not ver or not _repo_has_spec_version(present, name, ver):
            stale.append(name)
    _status_nl()
    return stale


def _spec_installable_in_yum(
    name: str,
    satisfied: set[str] | dict[str, set[str]] | None,
    spec_root: Path | None = None,
) -> bool:
    if not _spec_in_yum(name, satisfied, spec_root):
        return False
    targets = _satisfied_targets(satisfied)
    for target in targets:
        caps = _caps_for_target(satisfied, target)
        detail = _detail_for_target(satisfied, target)
        if detail is None or not caps:
            continue
        pkgs = [name]
        for pkg in _spec_yum_pkg_names(name, spec_root):
            if pkg in detail.requires:
                pkgs.append(pkg)
        for pkg in dict.fromkeys(pkgs):
            if pkg != name and not _yum_pkg_orders_spec(pkg, name) and pkg not in _spec_yum_pkg_names(name, spec_root):
                continue
            if pkg not in detail.requires and pkg != name:
                continue
            if not _rpm_runtime_ok(pkg, caps, detail):
                return False
    return True


def _spec_evr(path: Path) -> tuple[int, str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (0, "", "")
    preamble, _ = _split_spec_changelog(text)
    macros: dict[str, str] = {}
    for define in _RE_SPEC_DEFINE.finditer(preamble):
        val = (define.group("val") or "").strip()
        if val:
            macros[define.group("name")] = val

    def expand(val: str) -> str:
        for _ in range(8):
            nxt = re.sub(
                r"%\{([A-Za-z_][\w]*)\}",
                lambda m: macros[m.group(1)] if m.group(1) in macros else m.group(0),
                val,
            )
            if nxt == val:
                break
            val = nxt
        return val

    epoch = 0
    ver = ""
    m = re.search(r"(?im)^\s*Epoch\s*:\s*(\d+)", preamble)
    if m:
        epoch = int(m.group(1))
    m = _RE_SPEC_VERSION.search(preamble)
    if m:
        ver = expand(m.group("val").strip())
    return epoch, ver, ""


_PROVIDE_INDEX_MULTI_CACHE: dict[str, dict[str, set[str]]] = {}


def _provide_index_multi(spec_root: Path) -> dict[str, set[str]]:
    key = str(spec_root)
    hit = _PROVIDE_INDEX_MULTI_CACHE.get(key)
    if hit is not None:
        return hit
    out: dict[str, set[str]] = {}

    def add(atom: str, src: str) -> None:
        if atom and "%" not in atom:
            out.setdefault(atom.lower(), set()).add(src)

    try:
        kids = [p for p in spec_root.iterdir() if (p / (p.name + ".spec")).is_file()]
    except OSError:
        _PROVIDE_INDEX_MULTI_CACHE[key] = out
        return out
    for child in kids:
        src = child.name
        add(src, src)
        add(src + "-devel", src)
        add(src + "-prof", src)
    for child in kids:
        src = child.name
        meta = _spec_meta(child / (src + ".spec"))
        if meta is None:
            continue
        add(meta.name, src)
        add(meta.name + "-devel", src)
        for atom in meta.provides:
            add(atom, src)
            if atom.endswith("-devel"):
                add(atom[: -len("-devel")], src)
    _PROVIDE_INDEX_MULTI_CACHE[key] = out
    return out


def _provider_rank(src: str, atom: str, consumer: str | None = None) -> int:
    s = src.lower()
    a = atom.lower()
    cons = (consumer or "").lower()
    score = 0
    mingw_src = s.startswith("mingw")
    mingw_atom = a.startswith(("mingw", "mingw32-", "mingw64-"))
    mingw_cons = cons.startswith("mingw")
    if mingw_src and not mingw_atom and not mingw_cons:
        score -= 1000
    py = re.fullmatch(r"python3(?:\.\d+)?dist\(([^)]+)\)", a)
    if py:
        inner = re.sub(r"\[.*\]", "", py.group(1)).replace("_", "-")
        if s in {inner, "python-" + inner, "python3-" + inner}:
            score += 100
        elif s.startswith("python-") or s.startswith("python3-"):
            score += 20
        else:
            score -= 50
    if s == a or s + "-devel" == a or a == s + "-devel" or s + "-prof" == a:
        score += 80
    crate = re.fullmatch(r"crate\(([^)/]+)", a)
    if crate:
        inner = crate.group(1).replace("_", "-")
        if s in {"rust-" + inner, crate.group(1), "rust-" + crate.group(1)}:
            score += 100
    return score


def _provider_candidates(spec_root: Path, atom: str, idx: dict[str, str]) -> set[str]:
    multi = _provide_index_multi(spec_root)
    atom_l = atom.lower()
    cands: set[str] = set()
    for key in _lookup_keys(atom):
        kl = key.lower()
        if kl in _CHAIN_NOISE:
            continue
        hits = set(multi.get(kl) or ())
        if not hits:
            hit = idx.get(kl)
            if hit:
                hits.add(hit)
        for src in hits:
            if kl != atom_l and not _alias_provider_ok(src, atom_l, kl):
                continue
            cands.add(src)
    for guess in _soname_spec_guesses(atom):
        hit = idx.get(guess)
        if hit:
            cands.add(hit)
        if (spec_root / guess / (guess + ".spec")).is_file():
            cands.add(guess)
    return cands


_PICK_PROVIDER_CACHE: dict[tuple[str, str, str], str | None] = {}


def _pick_provider(
    spec_root: Path,
    atom: str,
    idx: dict[str, str],
    consumer: str | None = None,
) -> str | None:
    cache_key = (str(spec_root), atom.lower(), consumer or "")
    if cache_key in _PICK_PROVIDER_CACHE:
        return _PICK_PROVIDER_CACHE[cache_key]
    cands = _provider_candidates(spec_root, atom, idx)
    hit = None if not cands else max(cands, key=lambda src: (_provider_rank(src, atom, consumer), src))
    _PICK_PROVIDER_CACHE[cache_key] = hit
    return hit


def _find_provider_versioned(
    spec_root: Path,
    atom: str,
    op: str,
    ver: str,
    idx: dict[str, str],
    wants: list[tuple[str, str]] | None = None,
    consumer: str | None = None,
) -> str | None:
    cands = _provider_candidates(spec_root, atom, idx)
    if not cands:
        return None
    need = wants if wants else [(op, ver)]
    ok: list[tuple[tuple[int, str, str], str]] = []
    for src in cands:
        evr = _spec_evr(spec_root / src / (src + ".spec"))
        if evr[1] and _evr_matches_constraints(evr, need):
            ok.append((evr, src))
    if not ok:
        return None
    best = ok[0]
    for item in ok[1:]:
        r_best = _provider_rank(best[1], atom, consumer)
        r_new = _provider_rank(item[1], atom, consumer)
        if r_new != r_best:
            if r_new > r_best:
                best = item
            continue
        cmp = _evr_compare(item[0], best[0])
        if cmp > 0 or (cmp == 0 and item[1] > best[1]):
            best = item
    return best[1]


def _needed_br_provider(
    spec_root: Path,
    raw: str,
    alts: list[str],
    idx: dict[str, str],
    consumer: str,
    quick: bool = False,
) -> tuple[str | None, str]:
    wants_by: dict[str, list[tuple[str, str]]] = {}
    if not quick:
        for atom, op, ver in _br_all_constraints(raw):
            wants_by.setdefault(atom, []).append((op, ver))
            wants_by.setdefault(atom.lower(), []).append((op, ver))
    for atom in alts:
        wants = wants_by.get(atom) or wants_by.get(atom.lower()) or []
        provider = None
        if wants and not quick:
            provider = _find_provider_versioned(
                spec_root,
                atom,
                wants[0][0],
                wants[0][1],
                idx,
                wants=wants,
                consumer=consumer,
            )
        if provider is None:
            provider = _find_provider(spec_root, atom, idx, consumer)
        if provider:
            return provider, atom
    return None, alts[0] if alts else ""


def _find_provider(
    spec_root: Path,
    atom: str,
    idx: dict[str, str],
    consumer: str | None = None,
) -> str | None:
    return _pick_provider(spec_root, atom, idx, consumer)


def _alias_provider_ok(src: str, atom: str, key: str) -> bool:
    s = src.lower()
    a = atom.lower()
    k = key.lower()
    if s == k or s == a or s == k + "-devel" or a == s + "-devel" or s == k + "-prof":
        return True
    py = re.fullmatch(r"python3(?:\.\d+)?dist\(([^)]+)\)", a)
    if py:
        inner = re.sub(r"\[.*\]", "", py.group(1))
        dashed = inner.replace("_", "-")
        und = inner.replace("-", "_")
        return s in {
            inner,
            dashed,
            und,
            "python-" + inner,
            "python3-" + inner,
            "python-" + dashed,
            "python3-" + dashed,
            "python-" + und,
            "python3-" + und,
        }
    crate = re.fullmatch(r"crate\(([^)]+)\)", a)
    if crate:
        inner = crate.group(1).split("/", 1)[0]
        dashed = inner.replace("_", "-")
        return s in {"rust-" + dashed, "rust-" + inner}
    for suf in ("-devel", "-prof", "-doc", "-java", "-libs", "-static"):
        if s == k + suf or k == s + suf:
            return True
    return False


def _br_or_atoms(line: str) -> list[str]:
    s = line.strip()
    if not s:
        return []
    body = s[1:-1] if s.startswith("(") and s.endswith(")") else s
    if re.search(r"\s+or\s+", body, re.I):
        parts = re.split(r"\s+or\s+", body, flags=re.I)
        out: list[str] = []
        for part in parts:
            atoms = [
                a
                for a in _spec_dep_atoms(part)
                if a.lower() not in {"or", "and", "if", "else"}
                and a.lower() not in _CHAIN_NOISE
            ]
            if atoms:
                out.append(_normalize_py_atom(atoms[0]))
        return out
    return [
        _normalize_py_atom(a)
        for a in _spec_dep_atoms(body.split(" if ")[0] if " if " in body.lower() else body)
        if a.lower() not in _CHAIN_NOISE
    ]


def _normalize_py_atom(atom: str) -> str:
    m = re.fullmatch(r"python3(?:\.\d+)?dist\(([^)]+)\)", atom)
    if not m:
        return atom
    inner = re.sub(r"\[.*\]", "", m.group(1))
    return "python3dist(" + inner + ")"


def _rich_if_skip(line: str, satisfied: set[str] | None) -> bool:
    s = line.strip()
    if satisfied is None or not s.startswith("("):
        return False
    m = re.split(r"\s+if\s+", s[1:-1] if s.endswith(")") else s[1:], maxsplit=1, flags=re.I)
    if len(m) != 2:
        return False
    for atom in _spec_dep_atoms(m[1]):
        if _repo_has_atom(atom, satisfied):
            return True
    return False


def _distro_macro_loads(spec_root: Path, tmp: Path) -> list[str]:
    loads: list[str] = []
    rel = spec_root / "oreon-release" / "oreon-release.spec"
    if rel.is_file():
        try:
            text = rel.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        match = re.search(r"macros\.oreon-release << 'EOF'\n(.*)\nEOF", text, re.S)
        if match:
            dest = tmp / "macros.oreon-release"
            dest.write_text(match.group(1).replace("%%", "%") + "\n", encoding="utf-8")
            loads.append(str(dest.resolve()))
    for relpath in (
        "python-rpm-macros/macros.python-srpm",
        "python-rpm-macros/macros.python3",
        "ghc-srpm-macros/macros.ghc-srpm",
        "ghc-rpm-macros/macros.ghc-extra",
        "ghc-rpm-macros/macros.ghc",
    ):
        path = (spec_root / relpath).resolve()
        if path.is_file():
            loads.append(str(path))
    return loads


def _rpmspec_target_args(arch: str | None) -> list[str]:
    if not arch:
        return []
    return ["--target", "%s-redhat-linux" % arch]


def _rpmspec_distro_args() -> list[str]:
    return [
        "--undefine",
        "rhel",
        "--define",
        "oreon 11",
        "--define",
        "fedora 44",
        "--define",
        "__python /usr/bin/python3",
        "--define",
        "__python2 /usr/bin/python3",
        "--define",
        "__python3 /usr/bin/python3",
    ]


def _host_distro_undefines() -> list[str]:
    return _rpmspec_distro_args()


_RPMSPEC_CACHE: dict[tuple[Any, ...], list[str]] = {}
_RPMSPEC_FAILS = 0


def _spec_literal_buildrequires(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    pre, _ = _split_spec_changelog(text)
    out: list[str] = []
    for match in re.finditer(r"(?im)^BuildRequires:\s*(\S.*)$", pre):
        raw = match.group(1).strip()
        if not raw or raw.startswith("%"):
            continue
        out.append(raw)
    return out


def _rpmspec_query(
    path: Path,
    flag: str,
    load: list[str] | None = None,
    target: str | None = None,
    with_bconds: list[str] | None = None,
    without_bconds: list[str] | None = None,
) -> list[str]:
    if shutil.which("rpmspec") is None:
        raise click.ClickException("rpmspec is not installed")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    withs = tuple(with_bconds or ())
    withouts = tuple(without_bconds or ())
    cache_key = (str(path.resolve()), mtime, flag, target, withs, withouts)
    hit = _RPMSPEC_CACHE.get(cache_key)
    if hit is not None:
        return hit
    tgt = _rpmspec_target_args(target)
    extra: list[str] = []
    for name in withs:
        extra.extend(["--with", name])
    for name in withouts:
        extra.extend(["--without", name])
    cmd: list[str] = ["rpmspec", "-q", flag]
    cmd.extend(tgt)
    cmd.extend(_rpmspec_distro_args())
    cmd.extend(["--define", "_sourcedir %s" % path.parent])
    for macros in load or []:
        cmd.extend(["--load", macros])
    cmd.extend(extra)
    cmd.append(path.name)
    proc = subprocess.run(cmd, cwd=path.parent, capture_output=True, text=True)
    if proc.returncode != 0 and load:
        proc = subprocess.run(
            [
                "rpmspec",
                "-q",
                flag,
                *tgt,
                *_rpmspec_distro_args(),
                "--define",
                "_sourcedir %s" % path.parent,
                *extra,
                path.name,
            ],
            cwd=path.parent,
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        global _RPMSPEC_FAILS
        _RPMSPEC_FAILS += 1
        if flag == "--provides":
            _RPMSPEC_CACHE[cache_key] = []
            return []
        if flag == "--buildrequires":
            out = _spec_literal_buildrequires(path)
            _RPMSPEC_CACHE[cache_key] = out
            return out
        _RPMSPEC_CACHE[cache_key] = []
        return []
    out = [raw.strip() for raw in proc.stdout.splitlines() if raw.strip()]
    _RPMSPEC_CACHE[cache_key] = out
    return out


def _rpmspec_http_sources(path: Path, load: list[str] | None = None) -> list[str]:
    if shutil.which("rpmspec") is None:
        return []
    cmd = ["rpmspec", "-P", *_rpmspec_distro_args()]
    for macros in load or []:
        cmd.extend(["--load", macros])
    cmd.append(path.name)
    proc = subprocess.run(cmd, cwd=path.parent, capture_output=True, text=True)
    if proc.returncode != 0:
        cmd = ["rpmspec", "-P", *_rpmspec_distro_args(), path.name]
        proc = subprocess.run(cmd, cwd=path.parent, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    urls: list[str] = []
    for line in proc.stdout.splitlines():
        match = re.match(r"(?i)^Source\d*\s*:\s*(\S+)", line.strip())
        if not match:
            continue
        url = match.group(1).strip()
        if url.lower().startswith(("http://", "https://")):
            urls.append(url)
    return urls


def _rpmspec_buildrequires(
    path: Path,
    load: list[str] | None = None,
    target: str | None = None,
    with_bconds: list[str] | None = None,
    without_bconds: list[str] | None = None,
) -> list[str]:
    return _rpmspec_query(
        path,
        "--buildrequires",
        load,
        target=target,
        with_bconds=with_bconds,
        without_bconds=without_bconds,
    )


def _python3dist_req_variants(req: str) -> list[str]:
    m = re.search(r"python3(?:\.\d+)?dist\(([^)]+)\)", req)
    if not m:
        return [req]
    inner = re.sub(r"\[.*\]", "", m.group(1))
    variants = [inner, inner.replace("_", "-"), inner.replace("-", "_")]
    out: list[str] = []
    seen: set[str] = set()
    for inner2 in variants:
        text = req[: m.start()] + "python3dist(" + inner2 + ")" + req[m.end() :]
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _repo_pkg_count(value: object) -> int:
    if isinstance(value, _RepoDetail):
        return len(value.requires) or len(value.caps)
    if isinstance(value, set):
        return len(value)
    return 0


def _satisfied_targets(
    satisfied: set[str] | dict[str, set[str]] | None,
) -> list[str | None]:
    if not isinstance(satisfied, dict):
        return [None]
    keys = list(satisfied.keys())
    counts = [(t, _repo_pkg_count(satisfied[t])) for t in keys]
    max_n = max((n for _, n in counts), default=0)
    if max_n < _MIN_REPO_RPMS:
        return keys
    usable = [
        t
        for t, n in counts
        if n >= max(int(max_n * 0.3), _MIN_REPO_RPMS)
    ]
    return usable or keys


def _caps_for_target(
    satisfied: set[str] | dict[str, set[str]] | None,
    target: str | None,
) -> set[str] | None:
    if satisfied is None:
        return None
    if isinstance(satisfied, dict):
        if target is None:
            return None
        value = satisfied.get(target)
        if isinstance(value, _RepoDetail):
            return value.caps
        return value
    return satisfied


def _detail_for_target(
    satisfied: set[str] | dict[str, set[str]] | None,
    target: str | None,
) -> _RepoDetail | None:
    if not isinstance(satisfied, dict) or target is None:
        return None
    value = satisfied.get(target)
    return value if isinstance(value, _RepoDetail) else None


def _ghc_stem_provider(atom: str) -> str | None:
    m = re.match(r"libHS([A-Za-z0-9_.+-]+?)-\d[\d.]*-[A-Za-z0-9]{10,}", atom)
    if m:
        return "ghc-" + m.group(1)
    m = re.match(r"ghc-(?:devel|prof|doc)\(([A-Za-z0-9_.+-]+?)-\d[\d.]*-[A-Za-z0-9]{10,}\)", atom)
    if m:
        return "ghc-" + m.group(1)
    return None


def _soname_stem(atom: str) -> str | None:
    base = atom.split("(", 1)[0]
    i = base.find(".so")
    if i == -1:
        return None
    return base[: i + 3]


def _dso_family_in_repo(req: str, caps: set[str]) -> bool:
    if "(" not in req or ".so" not in req:
        return False
    stem = _soname_stem(req)
    if not stem:
        return False
    for cap in caps:
        other = _soname_stem(cap)
        if other == stem:
            return True
        base = cap.split("(", 1)[0]
        if base == stem or base.startswith(stem + "."):
            return True
    return False


def _yum_provider_rpms(atom: str, detail: "_RepoDetail") -> set[str]:
    pkgs: set[str] = set()
    for key in (atom, atom.lower()):
        pkgs |= set(detail.provides.get(key) or ())
    if not pkgs:
        return set()
    exact = {p for p in pkgs if p == atom or p.lower() == atom.lower()}
    if exact:
        return exact
    if len(pkgs) > 4:
        return set()
    return pkgs


def _rpmbuild_order_bin() -> str:
    exe = shutil.which("rpmbuild-order")
    if not exe:
        raise click.ClickException("rpmbuild-order is required for generate-chain")
    return exe


def _rpmbuild_order_rpmopts(
    spec_root: Path, arch: str | None, topdir: Path | None = None
) -> list[str]:
    out: list[str] = []
    undef = _host_distro_undefines()
    i = 0
    while i < len(undef):
        if undef[i] == "--undefine" and i + 1 < len(undef):
            out.extend(["-r", "--undefine=%s" % undef[i + 1]])
            i += 2
            continue
        if undef[i] == "--define" and i + 1 < len(undef):
            out.extend(["-r", "--define=%s" % undef[i + 1]])
            i += 2
            continue
        out.extend(["-r", undef[i]])
        i += 1
    if arch:
        out.extend(["-r", "--target=%s-redhat-linux" % arch])
    macdir = Path(tempfile.mkdtemp(prefix="orbscli-rbo-"))
    try:
        if topdir is not None:
            for sub in ("SOURCES", "BUILD", "SRPMS", "RPMS", "SPECS"):
                (topdir / sub).mkdir(parents=True, exist_ok=True)
            topmac = macdir / "macros.orbscli-topdir"
            topmac.write_text(
                "%%_topdir %s\n"
                "%%_sourcedir %%{_topdir}/SOURCES\n"
                "%%_builddir %%{_topdir}/BUILD\n"
                "%%_srcrpmdir %%{_topdir}/SRPMS\n"
                "%%_rpmdir %%{_topdir}/RPMS\n"
                % topdir,
                encoding="utf-8",
            )
            out.extend(["-r", "--load=%s" % topmac.resolve()])
        for macros in _distro_macro_loads(spec_root, macdir):
            out.extend(["-r", "--load=%s" % macros])
    except Exception:
        shutil.rmtree(macdir, ignore_errors=True)
        raise
    return out


def _rpmbuild_order_run(cwd: Path, args: list[str], env: dict[str, str] | None = None) -> str:
    cmd = [_rpmbuild_order_bin(), *args]
    click.echo(" ".join(cmd[:8]) + (" ..." if len(cmd) > 8 else ""), err=True)
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=run_env)
    if proc.returncode != 0:
        err = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        raise click.ClickException("rpmbuild-order failed: %s" % err[:4000])
    return proc.stdout


def _br_in_yum(
    raw: str,
    satisfied: set[str] | dict[str, set[str]] | None,
) -> bool:
    if satisfied is None:
        return False
    alts = _br_or_atoms(raw)
    if not alts:
        return True
    wants_by: dict[str, list[tuple[str, str]]] = {}
    for atom, op, ver in _br_all_constraints(raw):
        wants_by.setdefault(atom, []).append((op, ver))
        wants_by.setdefault(atom.lower(), []).append((op, ver))
    for target in _satisfied_targets(satisfied):
        caps = _caps_for_target(satisfied, target) or set()
        detail = _detail_for_target(satisfied, target)
        if not caps:
            return False
        ok = False
        for atom in alts:
            wants = wants_by.get(atom) or wants_by.get(atom.lower()) or []
            if _atom_installable(atom, wants, caps, detail):
                ok = True
                break
        if not ok:
            return False
    return True


_GENBR_MEM: dict[str, list[str]] = {}
_GENBR_CLIENT: httpx.Client | None = None


def _pep508_dist(token: str) -> str:
    name = token.strip()
    name = name.split(";", 1)[0]
    name = re.split(r"[<>=!~\[]", name, maxsplit=1)[0]
    name = name.strip().strip("\"'")
    if not name:
        return ""
    return re.sub(r"[-_.]+", "-", name).lower()


def _pyproject_extras_from_spec(text: str) -> list[str]:
    extras: list[str] = []
    for m in re.finditer(r"(?m)^%pyproject_buildrequires([^\n]*)", text):
        args = m.group(1).strip()
        if not args:
            continue
        x = re.search(r"(?:^|\s)-x\s+(\S+)", args)
        if x:
            extras.extend(p for p in x.group(1).split(",") if p)
        for tok in args.split():
            if tok.startswith("-"):
                continue
            extras.append(tok)
    return list(dict.fromkeys(extras))


def _pyproject_toml_brs(blob: str, extras: list[str]) -> list[str]:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore
    try:
        data = tomllib.loads(blob)
    except Exception:
        return []
    names: list[str] = []
    proj = data.get("project") or {}
    for item in proj.get("dependencies") or []:
        if isinstance(item, str):
            names.append(_pep508_dist(item))
    opt = proj.get("optional-dependencies") or {}
    want = set(extras) | {e.replace("_", "-") for e in extras}
    for extra, deps in opt.items():
        key = str(extra)
        if extras and key not in want and key.replace("_", "-") not in want:
            continue
        if not extras:
            continue
        for item in deps or []:
            if isinstance(item, str):
                names.append(_pep508_dist(item))
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    for item in (poetry.get("dependencies") or {}):
        if item != "python":
            names.append(_pep508_dist(str(item)))
    groups = poetry.get("group") or {}
    for extra in extras:
        deps = (groups.get(extra) or {}).get("dependencies") or {}
        for item in deps:
            names.append(_pep508_dist(str(item)))
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        out.append("python3dist(" + name + ")")
    return out


def _cargo_toml_dep_tables(data: dict[str, Any]) -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    deps = data.get("dependencies")
    if isinstance(deps, dict):
        for name, spec in deps.items():
            rows.append((str(name), spec))
    tgt = data.get("target")
    if isinstance(tgt, dict):
        for cfg, body in tgt.items():
            low = str(cfg).lower()
            if "windows" in low and "not" not in low:
                continue
            if not isinstance(body, dict):
                continue
            inner = body.get("dependencies")
            if isinstance(inner, dict):
                for name, spec in inner.items():
                    rows.append((str(name), spec))
    return rows


def _cargo_toml_brs(blob: str) -> list[str]:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore
    try:
        data = tomllib.loads(blob)
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for name, spec in _cargo_toml_dep_tables(data):
        crate = name
        default = True
        if isinstance(spec, dict):
            default = spec.get("default-features", True) is not False
        atom = "crate(%s/default)" % crate if default else "crate(%s)" % crate
        if atom not in seen:
            seen.add(atom)
            out.append(atom)
            if default:
                bare = "crate(%s)" % crate
                if bare not in seen:
                    seen.add(bare)
                    out.append(bare)
    return out


def _archive_best_member(names: list[str], want: str) -> str | None:
    hits = [n for n in names if n.replace("\\", "/").rstrip("/").endswith("/" + want) or n.replace("\\", "/").endswith(want)]
    hits = [n for n in hits if "/vendor/" not in n.replace("\\", "/") and "/target/" not in n.replace("\\", "/")]
    if not hits:
        return None
    hits.sort(key=lambda n: (n.count("/"), len(n)))
    return hits[0]


def _read_from_archive(path: Path, want: str) -> str | None:
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                member = _archive_best_member(zf.namelist(), want)
                if not member:
                    return None
                return zf.read(member).decode("utf-8", "replace")
        if tarfile.is_tarfile(path):
            with tarfile.open(path) as tf:
                member = _archive_best_member(tf.getnames(), want)
                if not member:
                    return None
                fh = tf.extractfile(member)
                if fh is None:
                    return None
                return fh.read().decode("utf-8", "replace")
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        return None
    return None


def _spec_source_archives(spec: Path) -> list[Path]:
    out: list[Path] = []
    for item in spec.parent.iterdir():
        if not item.is_file():
            continue
        n = item.name.lower()
        if n.endswith((".spec", ".diff", ".patch", ".asc", ".sig", ".service", ".conf")):
            continue
        if any(n.endswith(s) for s in (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".crate", ".zip", ".whl", ".tar")):
            out.append(item)
    return out


def _genbr_http() -> httpx.Client:
    global _GENBR_CLIENT
    if _GENBR_CLIENT is None:
        _GENBR_CLIENT = httpx.Client(timeout=120.0, follow_redirects=True)
    return _GENBR_CLIENT


def _generated_buildrequires(spec: Path, load: list[str] | None = None) -> list[str]:
    try:
        text = spec.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not re.search(r"(?m)^%(generate_buildrequires|gometa)\b", text):
        return []
    key = "%s:%s:%s" % (spec, spec.stat().st_mtime_ns, spec.stat().st_size)
    hit = _GENBR_MEM.get(key)
    if hit is not None:
        return hit
    rust = bool(re.search(r"(?m)^%cargo_generate_buildrequires\b", text)) or (
        spec.parent.name.startswith("rust-") and "%generate_buildrequires" in text
    )
    py = bool(re.search(r"(?m)^%pyproject_buildrequires\b", text))
    want = "Cargo.toml" if rust else ("pyproject.toml" if py else "")
    blob = None
    if want:
        for arch in _spec_source_archives(spec):
            blob = _read_from_archive(arch, want)
            if blob:
                break
        if blob is None and (spec.parent / want).is_file():
            blob = (spec.parent / want).read_text(encoding="utf-8", errors="replace")
        if blob is None:
            urls = _rpmspec_http_sources(spec, load)
            if not urls:
                pre, _ = _split_spec_changelog(text)
                found = _spec_literal_version(pre)
                ver = found[0] if found else ""
                macros = _spec_source_macros(text, ver or "0", spec.parent.name)
                urls = [u for _n, _r, u in _spec_http_sources(text, macros)]
            cache = Path.home() / ".cache" / "orbscli" / "src"
            cache.mkdir(parents=True, exist_ok=True)
            for url in urls:
                if "%" in url or not url.lower().startswith("http"):
                    continue
                fname = Path(url.split("?", 1)[0].split("#", 1)[0]).name or "src.bin"
                dest = cache / fname
                if not dest.is_file():
                    got = _fetch_source_archive(_genbr_http(), url, dest)
                    if not got:
                        continue
                blob = _read_from_archive(dest, want)
                if blob:
                    break
    out: list[str] = []
    if rust and blob:
        out = _cargo_toml_brs(blob)
    elif py and blob:
        out = _pyproject_toml_brs(blob, _pyproject_extras_from_spec(text))
    _GENBR_MEM[key] = out
    return out


def _pkg_chain_brs(
    spec: Path,
    load: list[str] | None,
    arch: str | None,
) -> list[str]:
    static = _rpmspec_buildrequires(spec, load, target=arch)
    if not static:
        static = list(_spec_buildrequires(spec))
    extra = _generated_buildrequires(spec, load)
    return list(dict.fromkeys([*static, *extra]))


def _pkg_unmet_req_atoms(pkg: str, caps: set[str], detail: "_RepoDetail") -> list[str]:
    out: list[str] = []
    or_groups: dict[str, list[str]] = {}
    for req, cond, grp in detail.requires.get(pkg, ()):
        if req.startswith(("/", "rpmlib(", "config(", "rtld(")):
            continue
        if cond is not None:
            continue
        if grp is not None:
            or_groups.setdefault(str(grp), []).append(req)
            continue
        if not _repo_has_atom(req, caps):
            out.append(req)
    for alts in or_groups.values():
        if alts and not any(_repo_has_atom(a, caps) for a in alts):
            out.append(alts[0])
    return out


def _yum_br_hole_providers(
    raw: str,
    satisfied: set[str] | dict[str, set[str]] | None,
    spec_root: Path,
    idx: dict[str, str],
    consumer: str,
) -> list[str]:
    if satisfied is None:
        return []
    alts = _br_or_atoms(raw)
    if not alts:
        return []
    wants_by: dict[str, list[tuple[str, str]]] = {}
    for atom, op, ver in _br_all_constraints(raw):
        wants_by.setdefault(atom, []).append((op, ver))
        wants_by.setdefault(atom.lower(), []).append((op, ver))
    holes: list[str] = []
    seen: set[str] = set()
    for target in _satisfied_targets(satisfied):
        caps = _caps_for_target(satisfied, target) or set()
        detail = _detail_for_target(satisfied, target)
        if detail is None or not caps:
            continue
        for atom in alts:
            wants = wants_by.get(atom) or wants_by.get(atom.lower()) or []
            if not _atom_repo_ok(atom, wants, caps, detail):
                continue
            if _atom_installable(atom, wants, caps, detail):
                continue
            pkgs = _yum_provider_rpms(atom, detail)
            if not pkgs:
                pkgs = set(detail.provides.get(atom) or ()) | set(
                    detail.provides.get(atom.lower()) or ()
                )
            for pkg in pkgs:
                for req in _pkg_unmet_req_atoms(pkg, caps, detail):
                    for req_alt in _br_or_atoms(req) or [req]:
                        provider, _a = _needed_br_provider(
                            spec_root, req, [req_alt], idx, consumer, quick=False
                        )
                        if provider and provider not in seen:
                            seen.add(provider)
                            holes.append(provider)
    return holes


def _rpmbuild_order_neighbor_names(
    spec_root: Path,
    seeds: list[str],
    satisfied: set[str] | dict[str, set[str]] | None,
    load: list[str] | None = None,
    arch: str | None = None,
    quick: bool = False,
) -> list[str]:
    idx = _provide_index(spec_root)
    seen: set[str] = set()
    queue: list[str] = []
    for name in seeds:
        if name not in seen:
            seen.add(name)
            queue.append(name)
    done = 0
    click.echo("neighbors from %d seeds" % len(queue), err=True)
    while queue:
        name = queue.pop()
        done += 1
        if done % 200 == 0:
            _status_line("neighbors %d queued %d" % (done, len(queue)))
        spec = spec_root / name / (name + ".spec")
        if not spec.is_file():
            continue
        brs = _pkg_chain_brs(spec, load, arch)
        for raw in brs:
            holes = _yum_br_hole_providers(raw, satisfied, spec_root, idx, name)
            for hole in holes:
                if hole not in seen:
                    seen.add(hole)
                    queue.append(hole)
            if _br_in_yum(raw, satisfied):
                continue
            if holes:
                continue
            alts = _br_or_atoms(raw)
            if not alts:
                continue
            provider, _atom = _needed_br_provider(
                spec_root, raw, alts, idx, name, quick=False
            )
            if not provider or provider in seen:
                continue
            seen.add(provider)
            queue.append(provider)
    _status_nl()
    return [n for n in seeds if n in seen] + [n for n in seen if n not in set(seeds)]



def _rpmbuild_order_link_pkg(spec_root: Path, work: Path, name: str) -> Path:
    src = spec_root / name
    dest = work / name
    dest.mkdir(exist_ok=True)
    for item in src.iterdir():
        target = dest / item.name
        if target.exists() or target.is_symlink():
            continue
        os.symlink(item, target)
    return dest


def _rpmbuild_order_stage_local_files(
    spec_root: Path, names: list[str], sourcedir: Path
) -> None:
    sourcedir.mkdir(parents=True, exist_ok=True)
    for name in names:
        src = spec_root / name
        if not src.is_dir():
            continue
        for item in src.iterdir():
            if not item.is_file():
                continue
            if item.name.endswith(".spec"):
                continue
            dest = sourcedir / item.name
            if dest.exists() or dest.is_symlink():
                continue
            os.symlink(item.resolve(), dest)


def _rpmbuild_order_stage_dyn_sources(
    spec_root: Path,
    work: Path,
    names: list[str],
    sourcedir: Path,
    load: list[str] | None = None,
) -> None:
    sourcedir.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, Path, Path]] = []
    for name in names:
        spec = spec_root / name / (name + ".spec")
        try:
            text = spec.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not re.search(r"(?m)^%(generate_buildrequires|gometa)\b", text):
            continue
        pkgdir = _rpmbuild_order_link_pkg(spec_root, work, name)
        urls = _rpmspec_http_sources(spec, load)
        if not urls:
            pre, _ = _split_spec_changelog(text)
            found = _spec_literal_version(pre)
            ver = found[0] if found else "0"
            macros = _spec_source_macros(text, ver, name)
            urls = [u for _n, _r, u in _spec_http_sources(text, macros)]
        for url in urls:
            if "%" in url:
                continue
            fname = Path(url.split("?", 1)[0]).name
            if not fname:
                continue
            shared = sourcedir / fname
            pkgfile = pkgdir / fname
            if shared.is_file() and pkgfile.is_file():
                continue
            jobs.append((url, shared, pkgfile))
    if not jobs:
        return
    click.echo("fetch Source0 for %d generate_buildrequires files" % len(jobs), err=True)
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        for url, shared, pkgfile in jobs:
            if not shared.is_file():
                got = _fetch_source_archive(client, url, shared)
                if not got:
                    raise click.ClickException("Source0 fetch failed for rpmbuild-order: %s" % url)
            if pkgfile.is_symlink() or pkgfile.exists():
                if pkgfile.is_file() and not pkgfile.is_symlink():
                    continue
                pkgfile.unlink()
            try:
                os.link(shared, pkgfile)
            except OSError:
                shutil.copy2(shared, pkgfile)


def _rpmbuild_order_missing_prep_rpms(
    spec_root: Path,
    names: list[str],
    satisfied: set[str] | dict[str, set[str]] | None,
    load: list[str] | None,
    arch: str | None,
) -> list[str]:
    if satisfied is None:
        return []
    target = arch if arch is not None else (_satisfied_targets(satisfied)[0] if satisfied else None)
    detail = _detail_for_target(satisfied, target)
    if not isinstance(detail, _RepoDetail):
        return []
    out: set[str] = set()
    for name in names:
        spec = spec_root / name / (name + ".spec")
        try:
            text = spec.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not re.search(r"(?m)^%(generate_buildrequires|gometa)\b", text):
            continue
        for raw in _rpmspec_buildrequires(spec, load, target=arch):
            for atom in _br_or_atoms(raw):
                atom = (atom or "").strip()
                if not atom or "(" in atom:
                    continue
                if atom.startswith("/"):
                    paths = [atom]
                    cmd = atom.rsplit("/", 1)[-1]
                else:
                    cmd = atom
                    paths = [
                        "/usr/bin/" + atom,
                        "/usr/sbin/" + atom,
                        "/bin/" + atom,
                        "/sbin/" + atom,
                    ]
                if not cmd or shutil.which(cmd):
                    continue
                hit = False
                for path in paths:
                    if path in detail.provides or path in detail.caps:
                        hit = True
                        out.update(detail.provides.get(path) or ())
                if not hit:
                    continue
                out.update(detail.provides.get(atom) or ())
                out.update(detail.provides.get(atom.lower()) or ())
    return sorted(p for p in out if p and "/" not in p)


def _dnf_download_rpms_to_dir(
    cmd: list[str], extra: list[str], rpmdir: Path, rpm_names: list[str]
) -> None:
    for pkg in rpm_names:
        proc = subprocess.run(
            cmd + extra + ["download", "--destdir", str(rpmdir), pkg],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0:
            raise click.ClickException(
                "dnf download failed for prep tool %s: %s"
                % (pkg, (proc.stderr or proc.stdout or "").strip()[:800])
            )


def _rpm_py_dep_packages(rpmdir: Path, detail: "_RepoDetail") -> list[str]:
    need: set[str] = set()
    for rpm in rpmdir.iterdir():
        if not rpm.name.endswith(".rpm") or rpm.name.endswith(".src.rpm"):
            continue
        proc = subprocess.run(
            ["rpm", "-qpR", str(rpm)], capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            extras: list[str] = []
            for m in re.finditer(r"python3(?:\.\d+)?dist\(([^)]+)\)", line):
                inner = m.group(1)
                extras.extend(re.findall(r"\[([^\]]+)\]", inner))
                bare = re.sub(r"\[.*\]", "", inner).strip()
                if bare:
                    need.update(_yum_provider_rpms("python3dist(" + bare + ")", detail))
            for extra in extras:
                for part in extra.split(","):
                    part = part.strip()
                    if part:
                        need.update(_yum_provider_rpms("python3dist(" + part + ")", detail))
            for atom in _br_or_atoms(line):
                if "dist(" not in atom:
                    continue
                need.update(_yum_provider_rpms(atom, detail))
    return sorted(need)


def _extract_rpm_cpio(rpm: Path, dest: Path) -> None:
    if rpm.name.endswith(".src.rpm"):
        return
    cpio = subprocess.run(["rpm2cpio", str(rpm)], capture_output=True, timeout=120)
    if cpio.returncode != 0:
        raise click.ClickException("rpm2cpio failed for %s" % rpm.name)
    unpack = subprocess.run(
        ["cpio", "-idmu", "--quiet", "--no-absolute-filenames", "-D", str(dest)],
        input=cpio.stdout,
        capture_output=True,
        timeout=120,
    )
    if unpack.returncode != 0:
        err = (unpack.stderr or b"").decode("utf-8", "replace")[:400]
        raise click.ClickException("cpio extract failed for %s: %s" % (rpm.name, err))


def _dnf_download_extract(
    repo_urls: list[str],
    arch: str | None,
    rpm_names: list[str],
    dest: Path,
    satisfied: set[str] | dict[str, set[str]] | None = None,
) -> None:
    if not repo_urls or not rpm_names:
        return
    dest.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="orbscli-dnf-dl-")
    try:
        root = Path(tmp) / "root"
        repos = Path(tmp) / "repos"
        cache = Path(tmp) / "cache"
        rpmdir = Path(tmp) / "rpms"
        root.mkdir()
        repos.mkdir()
        cache.mkdir()
        rpmdir.mkdir()
        rel = "11"
        for url in repo_urls:
            m = re.search(r"oreon-(\d+)", url)
            if m:
                rel = m.group(1)
                break
        cmd = _dnf_repoquery_cmd(root, repos, cache, rel, arch, repo_urls[0], "orbsdl0")
        extra: list[str] = []
        for i, url in enumerate(repo_urls[1:], start=1):
            rid = "orbsdl%s" % i
            extra.extend(
                [
                    "--repofrompath=%s,%s" % (rid, url.rstrip("/")),
                    "--enablerepo=%s" % rid,
                ]
            )
        _dnf_download_rpms_to_dir(cmd, extra, rpmdir, rpm_names)
        detail = _detail_for_target(satisfied, arch) if satisfied is not None else None
        if isinstance(detail, _RepoDetail):
            have = {n.split("-")[0] for n in rpm_names}
            more = [
                p
                for p in _rpm_py_dep_packages(rpmdir, detail)
                if p not in rpm_names
            ]
            if more:
                click.echo("yum prep python %s" % " ".join(more), err=True)
                _dnf_download_rpms_to_dir(cmd, extra, rpmdir, more)
        rpms = [p for p in rpmdir.glob("*.rpm") if not p.name.endswith(".src.rpm")]
        if not rpms:
            raise click.ClickException("dnf download wrote no rpms for %s" % " ".join(rpm_names))
        click.echo("yum prep tools %d rpms" % len(rpms), err=True)
        for rpm in rpms:
            _extract_rpm_cpio(rpm, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _rpmbuild_order_prep_env(prefix: Path, home: Path) -> dict[str, str]:
    bins = [
        prefix / "usr" / "bin",
        prefix / "usr" / "sbin",
        prefix / "bin",
        prefix / "sbin",
    ]
    path = os.pathsep.join(str(p) for p in bins if p.is_dir())
    if path:
        path = path + os.pathsep + os.environ.get("PATH", "")
    else:
        path = os.environ.get("PATH", "")
    py = [
        str(p)
        for p in list(prefix.glob("usr/lib/python*/site-packages"))
        + list(prefix.glob("usr/lib64/python*/site-packages"))
        if p.is_dir()
    ]
    ld = [
        str(p)
        for p in (prefix / "usr" / "lib64", prefix / "usr" / "lib")
        if p.is_dir()
    ]
    env = {"HOME": str(home), "PATH": path}
    if py:
        old = os.environ.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(py + ([old] if old else []))
    if ld:
        old = os.environ.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(ld + ([old] if old else []))
    return env


def _rbo_cycle_packages(text: str) -> list[str]:
    if "build dependency cycle" not in text:
        return []
    started = False
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        if "build dependency cycle" in line:
            started = True
            continue
        if not started:
            continue
        if line.strip().startswith("Shortest"):
            continue
        parts = line.split()
        if not parts or not all(re.fullmatch(r"[A-Za-z0-9._+-]+", p) for p in parts):
            continue
        for name in parts:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _reaches_self_in_set(name: str, deps: dict[str, set[str]], remaining: set[str]) -> bool:
    seen: set[str] = set()
    stack = list(deps[name] & remaining)
    while stack:
        cur = stack.pop()
        if cur == name:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(deps[cur] & remaining)
    return False


def _rbo_f44_cycle_pkgs(spec_root: Path, cycle_pkgs: list[str], already: list[str]) -> list[str]:
    have = set(already)
    out: list[str] = []
    for name in cycle_pkgs:
        if name in have:
            continue
        if (spec_root / name / (name + ".spec")).is_file():
            out.append(name)
    return out


def _order_dependency_layers(
    spec_root: Path,
    names: list[str],
    load: list[str] | None,
    arch: str | None,
    ignore_cycles: list[str] | None = None,
    quick: bool = False,
    satisfied: set[str] | dict[str, set[str]] | None = None,
) -> tuple[str, list[str]]:
    names = [n for n in names if (spec_root / n / (n + ".spec")).is_file()]
    if not names:
        return "", []
    idx = _provide_index(spec_root)
    name_set = set(names)
    deps: dict[str, set[str]] = {n: set() for n in names}
    click.echo("order %d pkgs" % len(names), err=True)
    for i, name in enumerate(names, 1):
        if i % 200 == 0 or i == len(names):
            _status_line("order %d/%d" % (i, len(names)))
        spec = spec_root / name / (name + ".spec")
        brs = _pkg_chain_brs(spec, load, arch)
        for raw in brs:
            alts = _br_or_atoms(raw)
            if not alts:
                continue
            provider, _atom = _needed_br_provider(
                spec_root, raw, alts, idx, name, quick=False
            )
            if provider and provider in name_set and provider != name:
                deps[name].add(provider)
            for hole in _yum_br_hole_providers(
                raw, satisfied, spec_root, idx, name
            ):
                if hole in name_set and hole != name:
                    deps[name].add(hole)
    _status_nl()
    ignore = set(ignore_cycles or ())
    if ignore:
        for name in names:
            deps[name] -= ignore
    remaining = set(names)
    layers: list[list[str]] = []
    cyclic: list[str] = []
    while remaining:
        ready = sorted(n for n in remaining if not (deps[n] & remaining))
        if not ready:
            cyclic = sorted(
                n for n in remaining if _reaches_self_in_set(n, deps, remaining)
            )
            if not cyclic:
                cyclic = sorted(remaining)
            break
        layers.append(ready)
        remaining.difference_update(ready)
    chain = " : ".join(" ".join(layer) for layer in layers if layer)
    return chain, cyclic


def _generate_chain_from_specs(
    spec_root: Path,
    seeds: list[str],
    satisfied: set[str] | dict[str, set[str]] | None,
    repo_urls: list[str] | None = None,
    repo_arch: str | None = None,
) -> tuple[str, list[str], str]:
    if not seeds:
        return "", [], ""
    for name in seeds:
        if not (spec_root / name / (name + ".spec")).is_file():
            raise click.ClickException("no spec for %s" % name)
    arch = repo_arch
    if arch is None and isinstance(satisfied, dict) and satisfied:
        arch = next(iter(satisfied))
    macdir = Path(tempfile.mkdtemp(prefix="orbscli-rbo-mac-"))
    try:
        load = _distro_macro_loads(spec_root, macdir)
    except Exception:
        shutil.rmtree(macdir, ignore_errors=True)
        raise
    click.echo("building chain from %d seeds" % len(seeds), err=True)
    neighbors = _rpmbuild_order_neighbor_names(
        spec_root, seeds, satisfied, load, arch, quick=False
    )
    click.echo("neighbors %d" % len(neighbors), err=True)
    _unused, cyclic = _order_dependency_layers(
        spec_root, neighbors, load, arch, quick=False, satisfied=satisfied
    )
    exclude = list(dict.fromkeys(cyclic))
    if exclude:
        click.echo("fedora-44 mock %s" % " ".join(exclude), err=True)
    chain_raw, _rest = _order_dependency_layers(
        spec_root,
        neighbors,
        load,
        arch,
        ignore_cycles=exclude,
        quick=False,
        satisfied=satisfied,
    )
    f44_raw = " ".join(exclude)
    shutil.rmtree(macdir, ignore_errors=True)
    chain = " ".join(chain_raw.split())
    selected = [p for p in chain.replace(":", " ").split() if p]
    return chain, selected, f44_raw


def _provide_atom(raw: str) -> str:
    s = raw.strip()
    if not s:
        return ""
    return re.split(r"\s*(?:=|>=|<=|==|<|>)\s*", s, maxsplit=1)[0].strip()


def _provide_evr_from_line(raw: str) -> tuple[int, str, str] | None:
    parts = re.split(r"\s*(?:=|>=|<=|==)\s*", raw.strip(), maxsplit=1)
    if len(parts) < 2:
        return None
    rest = parts[1].strip()
    epoch = 0
    if re.match(r"\d+:", rest):
        ep, rest = rest.split(":", 1)
        epoch = int(ep)
    if "-" in rest:
        ver, rel = rest.split("-", 1)
        return (epoch, ver, rel)
    return (epoch, rest, "")


def _ingest_require_line(raw: str, bucket: set[tuple[str, str | None, str | None]]) -> None:
    atom = raw.strip()
    if not atom:
        return
    low = atom.lower()
    if low.startswith(("rpmlib(", "config(", "rtld(")):
        return
    if atom.startswith("("):
        or_alts = _rich_or_alternatives(atom)
        if or_alts is not None:
            parts, cond = or_alts
            for part in parts:
                sub = part if part.startswith("(") else "(" + part + ")"
                for a, c in _rich_require_atoms(sub):
                    bucket.add((a, c or (cond.strip() if cond else None), atom))
        else:
            for a, c in _rich_require_atoms(atom):
                bucket.add((a, c, None))
        return
    bucket.add((_provide_atom(atom) or atom, None, None))


def _dnf_parse_repoquery(
    text: str,
) -> tuple[
    dict[str, set[str]],
    dict[str, set[tuple[str, str | None, str | None]]],
    dict[str, set[tuple[int, str, str]]],
    set[str],
]:
    provides: dict[str, set[str]] = {}
    requires: dict[str, set[tuple[str, str | None, str | None]]] = {}
    provides_evr: dict[str, set[tuple[int, str, str]]] = {}
    srpms: set[str] = set()
    chunks = text.split("===PKG===\n")
    for chunk in chunks:
        if not chunk.strip():
            continue
        if "\n--P--\n" not in chunk or "\n--Q--" not in chunk:
            continue
        head, rest = chunk.split("\n--P--\n", 1)
        prov_s, rest = rest.split("\n--Q--", 1)
        rest = rest[1:] if rest.startswith("\n") else rest
        if rest.startswith("--F--"):
            req_s, files_s = "", rest[len("--F--") :]
            files_s = files_s[1:] if files_s.startswith("\n") else files_s
        elif "\n--F--" in rest:
            req_s, files_s = rest.split("\n--F--", 1)
            files_s = files_s[1:] if files_s.startswith("\n") else files_s
        else:
            req_s, files_s = rest, ""
        meta = head.split("\n")
        if len(meta) < 4:
            continue
        name = meta[0].strip()
        if not name:
            continue
        ep, ver, rel = meta[1].strip(), meta[2].strip(), meta[3].strip()
        epoch = int(ep) if ep.isdigit() else 0
        evr = (epoch, ver, rel)
        if len(meta) > 4:
            src_name = meta[4].strip()
            if (
                src_name
                and "%" not in src_name
                and src_name.lower() not in {"(none)", "none", "(null)"}
            ):
                srpms.add(src_name)
            elif len(meta) > 5:
                src = _sourcerpm_to_name(meta[5])
                if src:
                    srpms.add(src)
            else:
                src = _sourcerpm_to_name(meta[4])
                if src:
                    srpms.add(src)
        provides.setdefault(name, set()).add(name)
        provides.setdefault(name.lower(), set()).add(name)
        provides_evr.setdefault(name, set()).add(evr)
        provides_evr.setdefault(name.lower(), set()).add(evr)
        for line in prov_s.splitlines():
            line = line.strip()
            if not line or line.startswith("("):
                continue
            atom = _provide_atom(line)
            if not atom:
                continue
            pevr = _provide_evr_from_line(line)
            aliases = [atom, atom.lower()]
            bare = _unflavor_rpm_atom(atom)
            if bare != atom:
                aliases.extend([bare, bare.lower()])
            for key in aliases:
                provides.setdefault(key, set()).add(name)
                if pevr:
                    provides_evr.setdefault(key, set()).add(pevr)
            py314 = re.fullmatch(r"python3\.\d+dist\(([^)]+)\)", atom)
            if py314:
                alias = "python3dist(" + py314.group(1) + ")"
                provides.setdefault(alias, set()).add(name)
                provides.setdefault(alias.lower(), set()).add(name)
                if pevr:
                    provides_evr.setdefault(alias, set()).add(pevr)
                    provides_evr.setdefault(alias.lower(), set()).add(pevr)
        bucket = requires.setdefault(name, set())
        for line in req_s.splitlines():
            _ingest_require_line(line.strip(), bucket)
        for line in files_s.splitlines():
            for path in line.split():
                if path.startswith("/"):
                    _yum_file_atoms(path, name, provides)
    return provides, requires, provides_evr, srpms


def _side_tag_info(api_base: str, tag_ref: str) -> tuple[str, int | None]:
    token = get_token()
    r = http_request_raw("GET", api_base, "/api/side-tags", token=token, timeout=30.0)
    if not r.is_success:
        if str(tag_ref).isdigit():
            raise click.ClickException("side tag lookup failed HTTP %s" % r.status_code)
        return tag_ref, None
    data: Any = r.json() if r.headers.get("content-type", "").startswith("application/json") else []
    items: list[Any]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = list(data.get("items") or data.get("side_tags") or [])
    else:
        items = []
    want = str(tag_ref)
    for it in items:
        if not isinstance(it, dict):
            continue
        if str(it.get("id")) == want or str(it.get("name") or "") == want:
            name = str(it.get("name") or "").strip()
            sid = it.get("id")
            tid = int(sid) if sid is not None else None
            if name:
                return name, tid
    if want.isdigit():
        raise click.ClickException("unknown side tag %s" % want)
    return tag_ref, None


def _side_tag_repo_dirs(side_tag_ref: str) -> list[str]:
    dirs: list[str] = []
    tag_name, _tag_id = _side_tag_info(get_api_url(), side_tag_ref)
    for item in (tag_name, side_tag_ref):
        name = str(item or "").strip()
        if not name or name.isdigit():
            continue
        if name not in dirs:
            dirs.append(name)
    if not dirs:
        raise click.ClickException("side tag %s has no repo name" % side_tag_ref)
    return dirs


def _repo_url(repo_base: str, release_ref: str, tag: str, arch: str) -> str:
    return "%s/%s/%s/%s" % (repo_base.rstrip("/"), release_ref, tag, arch)


def _repo_repomd_ok(url: str) -> bool:
    probe = url.rstrip("/") + "/repodata/repomd.xml"
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            r = client.head(probe)
            if r.status_code == 405:
                r = client.get(probe)
            return r.is_success
    except Exception:
        return False


def _dnf_repo_index(repo_urls: list[str], arch: str | None = None) -> set[str]:
    provides, _requires, _evr, _srpms = _dnf_repo_maps(repo_urls, arch)
    return set(provides)


def _parse_repo_arches(raw: str) -> list[str]:
    arches = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not arches:
        raise click.ClickException("need at least one arch")
    return arches


class _DnfSolver:
    def __init__(self, urls: list[str], arch: str | None, rel: str, tmp: str) -> None:
        import dnf

        self._dnf = dnf
        self._tmp = tmp
        self._cache: dict[str, tuple[bool, list[str]]] = {}
        root = Path(tmp) / "root"
        persist = Path(tmp) / "persist"
        (root / "var" / "lib" / "dnf").mkdir(parents=True, exist_ok=True)
        persist.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        logging.getLogger("dnf").setLevel(logging.CRITICAL)
        logging.getLogger("dnf.rpm").setLevel(logging.CRITICAL)
        self.base = dnf.Base()
        conf = self.base.conf
        conf.installroot = str(root)
        conf.persistdir = str(persist)
        conf.plugins = False
        conf.substitutions["releasever"] = rel
        conf.cachedir = str(Path(tmp) / "cache")
        conf.reposdir = str(Path(tmp) / "repos")
        conf.gpgcheck = False
        conf.sslverify = True
        if arch:
            conf.substitutions["arch"] = arch
            conf.substitutions["basearch"] = arch
        self.base.repos.clear()
        for i, url in enumerate(urls):
            self.base.repos.add_new_repo(
                "orbs%s" % i,
                conf,
                baseurl=[url.rstrip("/")],
                skip_if_unavailable=False,
            )
        self.base.fill_sack(load_system_repo=False, load_available_repos=True)

    def check_many(self, reqs: list[str]) -> tuple[bool, list[str]]:
        reqs = [r.strip() for r in reqs if (r or "").strip()]
        if not reqs:
            return True, []
        prepped: list[str] = []
        for req in reqs:
            hit = None
            for variant in _python3dist_req_variants(req):
                ok, _miss = self._check_one(variant)
                if ok:
                    hit = variant
                    break
            prepped.append(hit if hit is not None else req)
        key = "\n".join(prepped)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            result = self._resolve(prepped)
            self._cache[key] = result
            return result

    def check(self, req: str) -> tuple[bool, list[str]]:
        req = (req or "").strip()
        if not req:
            return True, []
        last: tuple[bool, list[str]] = (False, [req])
        for variant in _python3dist_req_variants(req):
            last = self._check_one(variant)
            if last[0]:
                return last
        return last

    def _check_one(self, req: str) -> tuple[bool, list[str]]:
        with self._lock:
            hit = self._cache.get(req)
            if hit is not None:
                return hit
            result = self._resolve([req])
            self._cache[req] = result
            return result

    def _resolve(self, reqs: list[str]) -> tuple[bool, list[str]]:
        ok = False
        missing: list[str] = []
        try:
            self.base.reset(goal=True)
            for req in reqs:
                self.base.install(req)
            self.base.resolve(allow_erasing=False)
            ok = True
        except Exception as exc:
            missing = _parse_dnf_missing(str(exc))
            if not missing:
                missing = list(reqs)
        return ok, missing

    def close(self) -> None:
        try:
            self.base.close()
        except Exception:
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)


def _parse_dnf_missing(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for pat in (
        re.compile(r"No matching package to install:\s+'([^']+)'"),
        re.compile(r"nothing provides\s+(\S+)"),
        re.compile(r"package ([^\s]+) is not installed"),
    ):
        for m in pat.finditer(text or ""):
            atom = m.group(1).strip()
            key = atom.lower()
            if atom and key not in seen:
                seen.add(key)
                out.append(atom)
    return out


class _RepoDetail:
    __slots__ = ("caps", "provides", "requires", "provides_evr", "srpms", "_req_met", "_inst", "solver")

    def __init__(
        self,
        caps: set[str],
        provides: dict[str, set[str]],
        requires: dict[str, set[tuple[str, str | None] | tuple[str, str | None, str | None]]],
        provides_evr: dict[str, set[tuple[int, str, str]]] | None = None,
        solver: "_DnfSolver | None" = None,
        srpms: set[str] | None = None,
    ) -> None:
        self.caps = caps
        self.provides = provides
        self.requires = {
            pkg: {r if len(r) == 3 else (r[0], r[1], None) for r in reqs}
            for pkg, reqs in requires.items()
        }
        self.provides_evr = provides_evr or {}
        self.srpms = set(srpms or ())
        self._req_met: dict[tuple[str, int], bool] = {}
        self._inst: dict[tuple[str, tuple[tuple[str, str], ...]], bool] = {}
        self.solver = solver


def _decompress_repo_blob(data: bytes, href: str) -> bytes:
    if href.endswith(".zst"):
        try:
            from compression import zstd as _pzstd

            return _pzstd.decompress(data)
        except ImportError:
            proc = subprocess.run(["zstd", "-dc"], input=data, capture_output=True, timeout=300)
            if proc.returncode != 0:
                raise click.ClickException("zstd decompress failed for %s" % href)
            return proc.stdout
    if href.endswith(".gz"):
        import gzip

        return gzip.decompress(data)
    if href.endswith(".xz"):
        import lzma

        return lzma.decompress(data)
    return data


def _split_rich_if(body: str) -> tuple[str, str | None]:
    depth = 0
    lower = body.lower()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and lower.startswith(" if ", i):
            return body[:i], body[i + 4:]
        i += 1
    return body, None


def _split_rich_or(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and body.startswith(" or ", i):
            parts.append(body[start:i])
            i += 4
            start = i
            continue
        i += 1
    parts.append(body[start:])
    return [p.strip() for p in parts if p.strip()]


def _rich_or_alternatives(expr: str) -> tuple[list[str], str | None] | None:
    s = expr.strip()
    body = s[1:-1] if s.startswith("(") and s.endswith(")") else s
    main, cond = _split_rich_if(body)
    while main.startswith("(") and main.endswith(")"):
        depth = 0
        wraps = True
        for i, ch in enumerate(main):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(main) - 1:
                    wraps = False
                    break
        if not wraps:
            break
        main = main[1:-1].strip()
    parts = _split_rich_or(main)
    if len(parts) < 2:
        return None
    return parts, cond


def _rich_require_atoms(expr: str) -> list[tuple[str, str | None]]:
    s = expr.strip()
    body = s[1:-1] if s.startswith("(") and s.endswith(")") else s
    main, cond = _split_rich_if(body)
    cond_expr = None
    if cond:
        cond_expr = cond.split(" unless ", 1)[0].strip() or None
    out: list[tuple[str, str | None]] = []
    for m in re.finditer(r"[a-zA-Z0-9_.+~-]+\([^()]*\)", main):
        out.append((m.group(0), cond_expr))
    masked = re.sub(r"[a-zA-Z0-9_.+~-]+\([^()]*\)", " ", main)
    for m in re.finditer(r"/[^\s()<>!=]+", masked):
        out.append((m.group(0), cond_expr))
    masked = re.sub(r"/[^\s()<>!=]+", " ", masked)
    masked = re.sub(r"(?:>=|<=|==|=|<|>)\s*\S+", " ", masked)
    for tok in re.findall(r"(?<![\w.+~-])([a-zA-Z_][\w.+~-]*)", masked):
        low = tok.lower()
        if low in {"and", "or", "with", "without", "if", "else", "then", "not", "unless"}:
            continue
        if low in _CHAIN_NOISE:
            continue
        out.append((tok, cond_expr))
    return out


def _rich_cond_holds(cond: str, detail: "_RepoDetail", seen: set[str]) -> bool:
    constraints = [(m.group(1), m.group(2), m.group(3)) for m in _RE_DEP_CONSTRAINT.finditer(cond)]
    if constraints:
        for atom, op, ver in constraints:
            evrs = detail.provides_evr.get(atom) or detail.provides_evr.get(atom.lower()) or set()
            if any(_constraint_ok(op, ver, evr) for evr in evrs):
                return True
        return False
    for atom in _spec_dep_atoms(cond):
        providers = set(detail.provides.get(atom, ())) | set(detail.provides.get(atom.lower(), ()))
        if providers & seen:
            return True
        if atom in seen or atom.lower() in {p.lower() for p in seen}:
            return True
    return False


def _repomd_href(repomd_text: str, data_type: str) -> str | None:
    block = re.search(
        r'<data type="%s">(.*?)</data>' % re.escape(data_type),
        repomd_text,
        re.S,
    )
    if not block:
        return None
    href_m = re.search(r'href="([^"]+)"', block.group(1))
    return href_m.group(1) if href_m else None


def _merge_repo_filelists(repo_url: str, provides: dict[str, set[str]]) -> None:
    root = repo_url.rstrip("/")
    try:
        click.echo("querying yum filelists", err=True)
        repomd = httpx.get(root + "/repodata/repomd.xml", timeout=60.0)
    except httpx.RequestError:
        return
    if repomd.status_code != 200:
        return
    href = _repomd_href(repomd.text, "filelists")
    if not href:
        return
    try:
        blob = httpx.get(root + "/" + href, timeout=600.0)
    except httpx.RequestError:
        return
    if blob.status_code != 200:
        return
    _ingest_filelists_blob(_decompress_repo_blob(blob.content, href), provides)


def _ingest_filelists_blob(data: bytes, provides: dict[str, set[str]]) -> None:
    import xml.etree.ElementTree as ET

    ns_f = "{http://linux.duke.edu/metadata/filelists}"
    for _event, elem in ET.iterparse(io.BytesIO(data), events=("end",)):
        if elem.tag != ns_f + "package":
            continue
        name = (elem.get("name") or "").strip()
        if name:
            for f in elem.findall(ns_f + "file"):
                path = (f.text or "").strip()
                if path:
                    _yum_file_atoms(path, name, provides)
        elem.clear()


def _repo_primary_maps(
    repo_urls: list[str],
    arch: str | None = None,
) -> tuple[
    dict[str, set[str]],
    dict[str, set[tuple[str, str | None, str | None]]],
    dict[str, set[tuple[int, str, str]]],
]:
    return _dnf_repo_maps(repo_urls, arch)


def _merge_dnf_parsed(
    dst: tuple[
        dict[str, set[str]],
        dict[str, set[tuple[str, str | None, str | None]]],
        dict[str, set[tuple[int, str, str]]],
        set[str],
    ],
    src: tuple[
        dict[str, set[str]],
        dict[str, set[tuple[str, str | None, str | None]]],
        dict[str, set[tuple[int, str, str]]],
        set[str],
    ],
) -> None:
    provides, requires, provides_evr, srpms = dst
    sp, sr, se, ss = src
    for key, pkgs in sp.items():
        provides.setdefault(key, set()).update(pkgs)
    for key, reqs in sr.items():
        requires.setdefault(key, set()).update(reqs)
    for key, evrs in se.items():
        provides_evr.setdefault(key, set()).update(evrs)
    srpms |= ss


def _dnf_repoquery_cmd(
    root: Path,
    repos: Path,
    cache: Path,
    rel: str,
    arch: str | None,
    url: str,
    rid: str,
) -> list[str]:
    cmd = [
        "dnf",
        "-q",
        "--installroot",
        str(root),
        "--releasever",
        rel,
        "--setopt=reposdir=%s" % repos,
        "--setopt=cachedir=%s" % cache,
        "--setopt=gpgcheck=0",
        "--disablerepo=*",
        "--repofrompath=%s,%s" % (rid, url.rstrip("/")),
        "--enablerepo=%s" % rid,
    ]
    if arch:
        cmd.extend(["--forcearch", arch])
    return cmd


def _dnf_repo_maps(
    repo_urls: list[str],
    arch: str | None = None,
    with_solver: bool = False,
) -> Any:
    if not repo_urls:
        if with_solver:
            return {}, {}, {}, set(), None
        return {}, {}, {}, set()
    if shutil.which("dnf") is None:
        raise click.ClickException("dnf is required to resolve generate-chain against the repo")
    tmp = tempfile.mkdtemp(prefix="orbscli-dnf-")
    solver = None
    try:
        root = Path(tmp) / "root"
        repos = Path(tmp) / "repos"
        cache = Path(tmp) / "cache"
        root.mkdir()
        repos.mkdir()
        cache.mkdir()
        rel = "11"
        for url in repo_urls:
            m = re.search(r"oreon-(\d+)", url)
            if m:
                rel = m.group(1)
                break
        qf = (
            "===PKG===\n%{name}\n%{epoch}\n%{version}\n%{release}\n%{source_name}\n%{sourcerpm}\n"
            "--P--\n%{provides}\n--Q--\n%{requires}\n--F--\n%{files}\n"
        )
        provides: dict[str, set[str]] = {}
        requires: dict[str, set[tuple[str, str | None, str | None]]] = {}
        provides_evr: dict[str, set[tuple[int, str, str]]] = {}
        srpms: set[str] = set()
        for i, url in enumerate(repo_urls):
            cmd = _dnf_repoquery_cmd(root, repos, cache, rel, arch, url, "orbs%s" % i)
            proc = subprocess.run(
                cmd + ["repoquery", "--available", "--qf", qf],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()[:400]
                raise click.ClickException("dnf repoquery failed: %s" % err)
            part = _dnf_parse_repoquery(proc.stdout)
            _merge_dnf_parsed((provides, requires, provides_evr, srpms), part)
            _merge_repo_filelists(url, provides)
            tag = url.rstrip("/").split("/")[-2] if "/" in url.rstrip("/") else url
            click.echo(
                "dnf %s %s %d rpms" % (arch or "", tag, len(part[1])),
                err=True,
            )
        if with_solver:
            click.echo("loading dnf solver %s" % (arch or ""), err=True)
            solver = _DnfSolver(repo_urls, arch, rel, tmp)
            return provides, requires, provides_evr, srpms, solver
        return provides, requires, provides_evr, srpms
    except Exception:
        if solver is None:
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        if not with_solver:
            shutil.rmtree(tmp, ignore_errors=True)


def _prompt_packages() -> list[str]:
    click.echo("packages")
    try:
        first = input()
    except EOFError:
        return []
    names = parse_package_blob(first)
    if len(names) > 1:
        return names
    lines = [first]
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return parse_package_blob("\n".join(lines))

def _toml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_key(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", name or ""):
        return name
    return _toml_quote(name)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return _toml_quote(str(value))


def _nvchecker_cmd() -> list[str]:
    exe = shutil.which("nvchecker")
    if exe:
        return [exe]
    try:
        import nvchecker  # noqa: F401
    except ImportError:
        raise click.ClickException(
            "nvchecker not found, install it (pip install 'nvchecker[pypi]' or dnf install nvchecker)"
        )
    return [sys.executable, "-m", "nvchecker"]


_RE_ATOM_TAG = r"<title>(?:v)?(\d[\w.+~-]*)</title>"


def _nv_normalize_version(ver: str) -> str:
    s = (ver or "").strip()
    if re.fullmatch(r"v\d.*", s, re.I):
        return s[1:]
    return s


def _nv_is_sane_version(ver: str) -> bool:
    s = (ver or "").strip()
    if not s or len(s) > 80:
        return False
    if re.search(r"[\s:/\\<>'\"#]", s):
        return False
    return bool(re.match(r"^\d[\w.+~-]*$", s))


def _spec_url_field(text: str) -> str | None:
    match = re.search(r"(?im)^URL\s*:\s*(\S+)", _split_spec_changelog(text)[0])
    return match.group(1).strip() if match else None


def _spec_probe_urls(pkg: str, text: str) -> list[str]:
    pre = _split_spec_changelog(text)[0]
    found = _spec_literal_version(pre)
    ver = found[0] if found else "0"
    macros = _spec_source_macros(text, ver, pkg)
    out: list[str] = []
    for _num, _raw, expanded in _spec_http_sources(text, macros):
        out.append(expanded)
    raw_url = _spec_url_field(text)
    if raw_url:
        out.append(_expand_source_macros(raw_url, macros))
    seen: set[str] = set()
    uniq: list[str] = []
    for url in out:
        key = url.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(key)
    return uniq


def _nv_version_regex_group(version: str) -> str:
    if re.fullmatch(r"\d{8}", version):
        return r"(\d{8})"
    if re.fullmatch(r"\d+", version):
        return r"(\d+)"
    if re.fullmatch(r"\d+(?:\.\d+)+[a-zA-Z]?\d*", version):
        return r"(\d+(?:\.\d+)+[a-zA-Z]?\d*)"
    return r"([0-9][0-9A-Za-z._+-]*)"


def _nvchecker_from_archive_url(url: str, version: str) -> dict[str, Any] | None:
    raw = (url or "").split("#", 1)[0].split("?", 1)[0].strip()
    if not raw.lower().startswith(("http://", "https://")):
        return None
    if "%{" in raw or "%(" in raw:
        return None
    if "/" not in raw:
        return None
    listing, fname = raw.rsplit("/", 1)
    if not fname or not version:
        return None
    if version not in fname:
        return None
    idx = fname.find(version)
    group = _nv_version_regex_group(version)
    regex = re.escape(fname[:idx]) + group + re.escape(fname[idx + len(version) :])
    return {"source": "regex", "url": listing + "/", "regex": regex}


def _github_repo_from_url(url: str) -> str | None:
    m = re.search(
        r"(?i)(?:https?://)?(?:www\.)?github\.com[/:]([^/\s]+)/([^/\s?#]+)",
        url or "",
    )
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    repo = re.sub(r"\.git$", "", repo, flags=re.I)
    if not owner or not repo or owner.lower() in {"settings", "orgs", "marketplace"}:
        return None
    return "%s/%s" % (owner, repo)


def _gitlab_repo_from_url(url: str) -> tuple[str, str] | None:
    m = re.search(
        r"(?i)(?:https?://)?([^/\s]+)/([^/\s]+)/([^/\s?#]+)",
        url or "",
    )
    if not m:
        return None
    host, owner, repo = m.group(1).lower(), m.group(2), m.group(3)
    repo = re.sub(r"\.git$", "", repo, flags=re.I)
    if "gitlab" not in host:
        return None
    return host, "%s/%s" % (owner, repo)


def _nvchecker_entry_from_local(spec_dir: Path, pkg: str) -> dict[str, Any] | None:
    import tomllib

    path = None
    for name in (".nvchecker.toml", "nvchecker.toml"):
        cand = spec_dir / name
        if cand.is_file():
            path = cand
            break
    if path is None:
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        click.echo("%s  bad %s (%s)" % (pkg, path.name, exc), err=True)
        return None
    entries = {k: v for k, v in data.items() if k != "__config__" and isinstance(v, dict)}
    if not entries:
        click.echo("%s  %s has no package entries" % (pkg, path.name), err=True)
        return None
    if pkg in entries:
        conf = dict(entries[pkg])
    elif len(entries) == 1:
        conf = dict(next(iter(entries.values())))
    else:
        click.echo(
            "%s  %s has multiple entries, add a [%s] table" % (pkg, path.name, pkg),
            err=True,
        )
        return None
    if not conf.get("source"):
        click.echo("%s  %s entry missing source=" % (pkg, path.name), err=True)
        return None
    if str(conf.get("source")).lower() in {"anitya", "repology"}:
        click.echo(
            "%s  refusing %s source in %s, use a direct upstream source" % (pkg, conf.get("source"), path.name),
            err=True,
        )
        return None
    return conf


def _nvchecker_guess_entry(pkg: str, text: str) -> dict[str, Any] | None:
    if "%{crates_source" in text or "%crates_source" in text or "%{crates_source}" in text:
        name = _spec_macro_line(text, "crate") or pkg.removeprefix("rust-")
        return {"source": "cratesio", "cratesio": name}
    if "%pypi_source" in text or "%{pypi_source" in text:
        name = (
            _spec_macro_line(text, "srcname")
            or _spec_macro_line(text, "pypi_name")
            or pkg.removeprefix("python-").removeprefix("python3-")
        )
        m = re.search(r"%\{pypi_source\s+([^\}]+)\}", text)
        if m:
            name = m.group(1).strip().split()[0]
        return {"source": "pypi", "pypi": name}

    kind = _lang2rpm_kind(pkg, text)
    upstream = _lang2rpm_upstream_name(kind, pkg, text) if kind else None
    if kind == "python" and upstream:
        return {"source": "pypi", "pypi": upstream}
    if kind == "rust" and upstream:
        return {"source": "cratesio", "cratesio": upstream}
    if kind == "node" and upstream:
        return {"source": "npm", "npm": upstream}
    if kind == "ruby" and upstream:
        return {"source": "gems", "gems": upstream}
    if kind == "perl" and upstream:
        return {"source": "cpan", "cpan": upstream}
    if kind == "r" and upstream:
        return {
            "source": "regex",
            "url": "https://cran.r-project.org/web/packages/%s/index.html" % upstream,
            "regex": r"Version:\s*([0-9][0-9A-Za-z._-]*)",
        }
    if kind == "haskell" and upstream:
        return {"source": "hackage", "hackage": upstream}

    urls = _spec_probe_urls(pkg, text)
    for url in urls:
        m = re.search(r"(?i)https?://download\.kde\.org/stable/release-service/", url)
        if m:
            return {
                "source": "regex",
                "url": "https://download.kde.org/stable/release-service/",
                "regex": r"release-service/(\d+\.\d+\.\d+)/",
            }
        m = re.search(r"(?i)https?://download\.gnome\.org/sources/([^/]+)/", url)
        if m:
            name = m.group(1)
            return {
                "source": "regex",
                "url": "https://download.gnome.org/sources/%s/cache.json" % name,
                "regex": r'"(\d+\.\d+(?:\.\d+)*)"',
            }
        m = re.search(r"(?i)gitlab\.gnome\.org/[^/\s]+/([^/\s?#]+)", url)
        if m:
            name = re.sub(r"\.git$", "", m.group(1), flags=re.I)
            return {
                "source": "regex",
                "url": "https://download.gnome.org/sources/%s/cache.json" % name,
                "regex": r'"(\d+\.\d+(?:\.\d+)*)"',
            }
    for url in urls:
        repo = _github_repo_from_url(url)
        if repo:
            return {
                "source": "regex",
                "url": "https://github.com/%s/tags.atom" % repo,
                "regex": _RE_ATOM_TAG,
            }
    for url in urls:
        gl = _gitlab_repo_from_url(url)
        if gl:
            host, repo = gl
            if "gnome.org" in host:
                continue
            return {
                "source": "regex",
                "url": "https://%s/%s/-/tags.atom" % (host, repo),
                "regex": _RE_ATOM_TAG,
            }
    for url in urls:
        m = re.search(r"(?i)pypi\.org/packages/source/./([^/]+)/", url)
        if m:
            return {"source": "pypi", "pypi": m.group(1)}
        m = re.search(r"(?i)files\.pythonhosted\.org/.*/([^/]+)-[\d.]", url)
        if m:
            return {"source": "pypi", "pypi": m.group(1)}
        m = re.search(r"(?i)crates\.io/(?:api/v1/)?crates/([^/]+)", url)
        if m:
            return {"source": "cratesio", "cratesio": m.group(1)}
        m = re.search(r"(?i)registry\.npmjs\.org/([^/]+)/-/", url)
        if m:
            return {"source": "npm", "npm": m.group(1)}
        m = re.search(r"(?i)rubygems\.org/gems/([^/]+)", url)
        if m:
            return {"source": "gems", "gems": m.group(1)}
        m = re.search(r"(?i)(?:metacpan\.org|cpan\.metacpan\.org|www\.cpan\.org)/.*?/([^/]+)", url)
        if m and pkg.startswith("perl-"):
            return {"source": "cpan", "cpan": m.group(1).replace("-", "::")}
    for url in urls:
        if re.search(r"(?i)(?:cdn\.)?kernel\.org/pub/linux/kernel/firmware/", url):
            return {
                "source": "regex",
                "url": "https://cdn.kernel.org/pub/linux/kernel/firmware/",
                "regex": r"linux-firmware-(\d{8})\.tar\.xz",
            }
    for url in urls:
        m = re.search(
            r"(?i)(?:cdn\.)?kernel\.org/pub/linux/kernel/(v\d+\.x)/",
            url,
        )
        if m:
            series = m.group(1)
            return {
                "source": "regex",
                "url": "https://cdn.kernel.org/pub/linux/kernel/%s/" % series,
                "regex": r"linux-(\d+\.\d+\.\d+)\.tar\.xz",
            }
    if pkg == "kernel" or any(
        re.search(r"(?i)/linux/kernel/v\d+\.x/linux-", u) for u in urls
    ):
        return {
            "source": "regex",
            "url": "https://www.kernel.org/feeds/kdist.xml",
            "regex": r"stable.*?(\d+\.\d+\.\d+)",
        }
    found = _spec_literal_version(_split_spec_changelog(text)[0])
    ver = found[0] if found else ""
    for url in urls:
        conf = _nvchecker_from_archive_url(url, ver)
        if conf:
            return conf
    return None


def _nvchecker_resolve_entry(pkg: str, spec_path: Path, text: str) -> dict[str, Any] | None:
    local = _nvchecker_entry_from_local(spec_path.parent, pkg)
    if local is not None:
        return local
    return _nvchecker_guess_entry(pkg, text)


def _write_nvchecker_toml(
    path: Path,
    entries: dict[str, dict[str, Any]],
    oldver: Path,
    newver: Path,
) -> None:
    lines = [
        "[__config__]",
        "oldver = %s" % _toml_quote(str(oldver)),
        "newver = %s" % _toml_quote(str(newver)),
        "max_concurrency = 100",
        "http_timeout = 5",
        "",
    ]
    for name, conf in entries.items():
        lines.append("[%s]" % _toml_key(name))
        for key, value in conf.items():
            if value is None:
                continue
            lines.append("%s = %s" % (key, _toml_value(value)))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _ver_rank(ver: str) -> tuple[tuple[int, ...], str]:
    nums = tuple(int(x) for x in re.findall(r"\d+", ver)[:8])
    return nums, ver


def _best_regex_version(text: str, pattern: str) -> str:
    try:
        rx = re.compile(pattern)
    except re.error:
        return ""
    found = [_nv_normalize_version(m.group(1)) for m in rx.finditer(text) if m.lastindex]
    found = [v for v in found if v and _nv_is_sane_version(v)]
    if not found:
        return ""
    return max(found, key=_ver_rank)


def _entry_http_job(conf: dict[str, Any]) -> tuple[str, str, str] | None:
    src = str(conf.get("source") or "").lower()
    if src == "pypi":
        name = str(conf.get("pypi") or "").strip()
        if not name:
            return None
        return ("json", "https://pypi.org/pypi/%s/json" % name, "pypi")
    if src == "cratesio":
        name = str(conf.get("cratesio") or "").strip()
        if not name:
            return None
        return ("json", "https://crates.io/api/v1/crates/%s" % name, "crates")
    if src == "npm":
        name = str(conf.get("npm") or "").strip()
        if not name:
            return None
        return ("json", "https://registry.npmjs.org/%s" % name, "npm")
    if src == "gems":
        name = str(conf.get("gems") or "").strip()
        if not name:
            return None
        return ("json", "https://rubygems.org/api/v1/versions/%s/latest.json" % name, "gems")
    if src == "cpan":
        name = str(conf.get("cpan") or "").strip().replace("::", "-")
        if not name:
            return None
        return ("json", "https://fastapi.metacpan.org/v1/release/%s" % name, "cpan")
    if src == "hackage":
        name = str(conf.get("hackage") or "").strip()
        if not name:
            return None
        return ("json", "https://hackage.haskell.org/package/%s/preferred.json" % name, "hackage")
    if src == "github":
        repo = str(conf.get("github") or "").strip()
        if not repo:
            return None
        return (
            "regex",
            "https://github.com/%s/tags.atom" % repo,
            _RE_ATOM_TAG,
        )
    if src == "gitlab":
        repo = str(conf.get("gitlab") or "").strip()
        host = str(conf.get("host") or "gitlab.com").strip()
        if not repo:
            return None
        return (
            "regex",
            "https://%s/%s/-/tags.atom" % (host, repo),
            _RE_ATOM_TAG,
        )
    if src == "git":
        git = str(conf.get("git") or "")
        repo = _github_repo_from_url(git)
        if repo:
            return (
                "regex",
                "https://github.com/%s/tags.atom" % repo,
                _RE_ATOM_TAG,
            )
        gl = _gitlab_repo_from_url(git)
        if gl:
            host, repo = gl
            if "gnome.org" in host:
                return None
            return (
                "regex",
                "https://%s/%s/-/tags.atom" % (host, repo),
                _RE_ATOM_TAG,
            )
        return None
    if src == "regex":
        url = str(conf.get("url") or "").strip()
        pattern = str(conf.get("regex") or "")
        if not url or not pattern:
            return None
        return ("regex", url, pattern)
    return None


def _json_version(kind: str, payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    if kind == "pypi":
        info = payload.get("info")
        if isinstance(info, dict):
            return _nv_normalize_version(str(info.get("version") or ""))
    if kind == "crates":
        crate = payload.get("crate")
        if isinstance(crate, dict):
            return _nv_normalize_version(
                str(crate.get("max_stable_version") or crate.get("max_version") or "")
            )
    if kind == "npm":
        tags = payload.get("dist-tags")
        if isinstance(tags, dict):
            return _nv_normalize_version(str(tags.get("latest") or ""))
    if kind == "gems":
        return _nv_normalize_version(str(payload.get("version") or ""))
    if kind == "cpan":
        return _nv_normalize_version(str(payload.get("version") or ""))
    if kind == "hackage":
        normal = payload.get("normal-version")
        if isinstance(normal, list) and normal:
            return _nv_normalize_version(str(normal[0]))
    return ""


def _anitya_items_to_map(items: list[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        ver = _nv_normalize_version(
            str(item.get("stable_version") or item.get("version") or "")
        )
        if name and ver and _nv_is_sane_version(ver):
            out[name] = ver
    return out


def _anitya_cache_path() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "orbscli" / "anitya-fedora.json"


def _anitya_fedora_versions() -> dict[str, str]:
    path = _anitya_cache_path()
    if path.is_file() and time.time() - path.stat().st_mtime < 6 * 3600:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data:
            return dict((str(k), str(v)) for k, v in data.items() if v)
    click.echo("index fedora upstream versions", err=True)
    url = "https://release-monitoring.org/api/v2/packages/"
    headers = {"User-Agent": "orbscli-spec-bump", "Accept": "application/json"}

    async def _go() -> dict[str, str]:
        timeout = httpx.Timeout(20.0, connect=5.0)
        limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)
        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            headers=headers,
            follow_redirects=True,
        ) as client:
            first = await client.get(
                url,
                params={"distribution": "Fedora", "items_per_page": 250, "page": 1},
            )
            first.raise_for_status()
            payload = first.json()
            total = int(payload.get("total_items") or 0)
            pages = max(1, (total + 249) // 250)
            items = list(payload.get("items") or [])
            _status_line("index 1/%d" % pages)
            sem = asyncio.Semaphore(8)

            async def page(n: int) -> list[Any]:
                async with sem:
                    resp = await client.get(
                        url,
                        params={
                            "distribution": "Fedora",
                            "items_per_page": 250,
                            "page": n,
                        },
                    )
                    resp.raise_for_status()
                    return list(resp.json().get("items") or [])

            if pages > 1:
                done = 1
                for chunk in asyncio.as_completed(
                    [page(n) for n in range(2, pages + 1)]
                ):
                    items.extend(await chunk)
                    done += 1
                    _status_line("index %d/%d" % (done, pages))
            return _anitya_items_to_map(items)

    try:
        out = asyncio.run(_go())
    except Exception as exc:
        _status_nl()
        click.echo("upstream index failed (%s)" % exc, err=True)
        return {}
    _status_nl()
    if out:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out), encoding="utf-8")
    return out


def _nvchecker_run(entries: dict[str, dict[str, Any]]) -> dict[str, str]:
    if not entries:
        return {}
    jobs = {name: _entry_http_job(conf) for name, conf in entries.items()}
    jobs = {name: job for name, job in jobs.items() if job}

    async def _go() -> dict[str, str]:
        out: dict[str, str] = {}
        total = len(jobs)
        done = 0
        last_print = 0
        netn = _host_workers("net")
        _status_line("check 0/%d (%d http)" % (total, netn))
        timeout = httpx.Timeout(2.0, connect=0.8)
        limits = httpx.Limits(max_connections=netn, max_keepalive_connections=netn)
        headers = {"User-Agent": "orbscli-spec-bump", "Accept": "*/*"}
        sem = asyncio.Semaphore(netn)

        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            headers=headers,
            follow_redirects=True,
        ) as client:

            async def one(name: str, job: tuple[str, str, str]) -> None:
                nonlocal done, last_print
                kind, url, extra = job
                ver = ""
                async with sem:
                    try:
                        resp = await client.get(url)
                        if resp.status_code == 429:
                            await asyncio.sleep(0.4)
                            resp = await client.get(url)
                        if resp.status_code < 400:
                            if kind == "json":
                                ver = _json_version(extra, resp.json())
                            else:
                                ver = _best_regex_version(resp.text, extra)
                    except Exception:
                        ver = ""
                if ver and _nv_is_sane_version(ver):
                    out[name] = ver
                done += 1
                if done - last_print >= 50 or done == total:
                    _status_line("check %d/%d" % (done, total))
                    last_print = done

            names = list(jobs)
            for i in range(0, len(names), 32):
                batch = names[i : i + 32]
                await asyncio.gather(*(one(n, jobs[n]) for n in batch))
        return out

    got = asyncio.run(_go())
    _status_nl()
    return got


def _nvchecker_run_batched(
    entries: dict[str, dict[str, Any]], batch: int = 0, workers: int = 0
) -> dict[str, str]:
    if not entries:
        return {}
    click.echo("check %d versions" % len(entries), err=True)
    bulk = _anitya_fedora_versions()
    out: dict[str, str] = {}
    rest: dict[str, dict[str, Any]] = {}
    for name, conf in entries.items():
        ver = bulk.get(name)
        if ver and _nv_is_sane_version(ver):
            out[name] = ver
        else:
            rest[name] = conf
    click.echo("index hit %d  miss %d" % (len(out), len(rest)), err=True)
    if rest:
        out.update(_nvchecker_run(rest))
    empty = len(entries) - len(out)
    if empty:
        click.echo("no version for %d packages" % empty, err=True)
    return out


def _spec_dirs_with_specs(root: Path) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        spec = child / (child.name + ".spec")
        if spec.is_file():
            out.append((child.name, spec))
            continue
        specs = [p for p in child.glob("*.spec") if p.is_file()]
        if len(specs) == 1:
            out.append((child.name, specs[0]))
    return out


def _nevra_to_name(nevra: str) -> str:
    s = nevra.strip()
    for arch in (".x86_64", ".aarch64", ".ppc64le", ".s390x", ".noarch", ".i686"):
        if s.endswith(arch):
            s = s[: -len(arch)]
            break
    m = re.match(r"^(?:([^:]+):)?(.+)-([^-]+-[^-]+)$", s)
    if m:
        return m.group(2)
    if s.count("-") >= 2:
        return s.rsplit("-", 2)[0]
    return s

def _spec_dir_for_rpm(spec_root: Path, rpm_name: str) -> str | None:
    if (spec_root / rpm_name).is_dir():
        p = spec_root / rpm_name
        if (p / f"{rpm_name}.spec").is_file():
            return rpm_name
        specs = list(p.glob("*.spec"))
        if len(specs) == 1:
            return rpm_name
    if rpm_name.startswith("python3-"):
        alt = "python-" + rpm_name[len("python3-") :]
        if (spec_root / alt).is_dir() and (
            (spec_root / alt / f"{alt}.spec").is_file() or len(list((spec_root / alt).glob("*.spec"))) == 1
        ):
            return alt
    return None

def resolve_release_id(api_base: str, token: str | None, release: str) -> int:
    release = release.strip()
    if release.isdigit():
        return int(release)
    out = http_json("GET", api_base, "/api/releases", token=token, params={"limit": 1000, "offset": 0})
    for item in out.get("items", []):
        if item.get("releasename") == release:
            return int(item["id"])
    click.echo("Release not found: %s" % release, err=True)
    sys.exit(1)

def find_package_by_name(api_base: str, token: str | None, name: str) -> dict[str, Any] | None:
    out = http_json(
        "GET",
        api_base,
        "/api/packages",
        token=token,
        params={"search": name, "limit": 100, "offset": 0},
    )
    exact = None
    for p in out.get("items", []):
        if p.get("name") == name:
            exact = p
            break
    return exact

def distgit_new_package_payload(
    pkg: str,
    releasename: str,
    *,
    distgit_url: str,
    distgit_branch: str,
) -> dict[str, Any]:
    spec_rel = f"{releasename}/{pkg}/{pkg}.spec"
    url = f"{distgit_url.rstrip('/')}#{distgit_branch}:{spec_rel}"
    return {
        "name": pkg,
        "description": None,
        "preferred_mock_environment_id": None,
        "github_repository_id": None,
        "github_html_url": "https://github.com/oreonhq/rpm-specfiles",
        "source_type": "distgit",
        "source_url": url,
        "source_urls": None,
        "source_branch": distgit_branch,
        "source_path": None,
        "version": "0.0.1",
    }

@click.group()
@click.option(
    "--api-url",
    envvar="ORBSCLI_API_URL",
    default=None,
    help="Build API base URL (saved on login unless --no-save). Also OREON_API_URL.",
)
@click.option(
    "--watchdog-url",
    envvar="ORBSCLI_WATCHDOG_URL",
    default=None,
    help="Watchdog base URL for advisory commands. Default http://localhost:8001",
)
@click.version_option(version="1.0.0", prog_name="orbscli")
@click.pass_context
def cli(ctx: click.Context, api_url: str | None, watchdog_url: str | None) -> None:
    ctx.ensure_object(dict)
    ctx.obj["API_URL"] = api_url
    ctx.obj["WATCHDOG_URL"] = watchdog_url

@cli.command("login")
@click.option("--url", "api_url", default=None, help="API base URL, e.g. https://build.example.com")
@click.option("-u", "--username", required=True)
@click.option("-p", "--password", envvar="ORBSCLI_PASSWORD", default=None, help="Password (use env ORBSCLI_PASSWORD)")
@click.option("--watchdog-url", default=None, help="Also store Watchdog base URL in config")
@click.option("--no-save", is_flag=True, help="Print token only, do not write credentials to disk")
@click.pass_context
def cmd_login(
    ctx: click.Context,
    api_url: str | None,
    username: str,
    password: str | None,
    watchdog_url: str | None,
    no_save: bool,
) -> None:
    base = (api_url or get_api_url()).rstrip("/")
    if not password:
        click.echo("Password required (-p or ORBSCLI_PASSWORD).", err=True)
        sys.exit(2)
    out = http_json(
        "POST",
        base,
        "/api/auth/login",
        json_body={"username": username, "password": password},
        token=None,
    )
    token = out.get("access_token")
    if not token:
        click.echo("Login response missing access_token", err=True)
        sys.exit(1)
    if no_save:
        click.echo(token)
        return
    _ensure_config_dir()
    _save_json(CREDENTIALS_PATH, {"access_token": token})
    cfg = _load_json(CONFIG_PATH)
    cfg["api_url"] = base
    if watchdog_url:
        cfg["watchdog_url"] = watchdog_url.rstrip("/")
    _save_json(CONFIG_PATH, cfg)
    click.echo("Logged in as %s. Token stored in %s" % (username, CREDENTIALS_PATH))

@cli.command("logout")
def cmd_logout() -> None:
    if CREDENTIALS_PATH.is_file():
        CREDENTIALS_PATH.unlink()
    click.echo("Logged out (credentials removed).")

@cli.command("whoami")
def cmd_whoami() -> None:
    token = require_auth(get_token())
    p = _jwt_payload_unverified(token)
    click.echo("user: %s\nrole: %s" % (p.get("sub", "?"), p.get("role", "?")))

@cli.command("spec-bump")
@click.option(
    "-p",
    "--packages",
    "packages_blob",
    default=None,
    help="Package list (spaces, commas, or newlines). Skip the packages prompt.",
)
@click.option(
    "-d",
    "--spec-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="rpm-specfiles tree. Skip the directory prompt.",
)
@click.option(
    "--all",
    "bump_all",
    is_flag=True,
    default=False,
    help="Bump every package directory with a spec under --spec-root.",
)
@click.option(
    "--verify-sources/--no-verify-sources",
    default=None,
    help="HEAD-check every remote Source URL after the version bump. Off with --all unless passed.",
)
@click.option(
    "--refresh-hashes/--no-refresh-hashes",
    default=None,
    help="Download Source0 and refresh the source0_hash guard. Off with --all unless passed.",
)
@click.option(
    "--verify-patches/--no-verify-patches",
    default=True,
    help="Unpack Source0 and check every Patch applies. On by default, including --all.",
)
@click.option(
    "--refresh-configs/--no-refresh-configs",
    default=None,
    help="For kernel, merge new Kconfig symbols into sorted Source configs (never rewrite full dumps).",
)
@click.option(
    "--refresh-files/--no-refresh-files",
    default=True,
    help="Add missing %%files entries from the new Source0 tree. On by default, including --all.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="Never prompt; unfixable sources fail instead of asking for a URL.",
)
def cmd_spec_bump(
    packages_blob: str | None,
    spec_root: Path | None,
    bump_all: bool,
    verify_sources: bool,
    refresh_hashes: bool,
    verify_patches: bool,
    refresh_configs: bool,
    refresh_files: bool,
    non_interactive: bool,
) -> None:
    """Bump spec versions from upstream."""
    try:
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    if bump_all:
        non_interactive = True
    if verify_sources is None:
        verify_sources = not bump_all
    if refresh_hashes is None:
        refresh_hashes = not bump_all
    if refresh_configs is None:
        refresh_configs = not bump_all
    try:
        _lang2rpm_ensure_host_tools()
    except click.ClickException as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    if spec_root is None:
        spec_root = Path(__file__).resolve().parent.parent / "fedora-rpms" / "oreon-11-rp1"
    root = spec_root.expanduser()
    if not root.is_dir():
        click.echo("not a directory  %s" % root, err=True)
        sys.exit(2)
    if bump_all:
        packed = _spec_dirs_with_specs(root)
        names = [n for n, _p in packed]
        spec_by_name = {n: p for n, p in packed}
        click.echo("scan %d spec dirs under %s" % (len(names), root), err=True)
        if not names:
            click.echo("no spec directories under %s" % root, err=True)
            sys.exit(2)
    else:
        spec_by_name = {}
        if packages_blob is None:
            if not sys.stdin.isatty():
                click.echo("pass -p/--packages when stdin is not a tty", err=True)
                sys.exit(2)
            names = _prompt_packages()
        else:
            names = parse_package_blob(packages_blob)
        if not names:
            click.echo("no packages given", err=True)
            sys.exit(2)

    ok = 0
    skip = 0
    fail = 0
    nosource = 0
    work: list[tuple[str, Path]] = []
    entries: dict[str, dict[str, Any]] = {}
    jobs: list[tuple[str, Path]] = []
    for pkg in names:
        if pkg in spec_by_name:
            specs = [spec_by_name[pkg]]
        else:
            specs = find_pkg_specs(root, pkg)
        if not specs:
            click.echo("%s  no spec under %s" % (pkg, root), err=True)
            fail += 1
            continue
        if len(specs) > 1:
            click.echo("%s  multiple specs, pick a tighter directory" % pkg, err=True)
            for p in specs:
                click.echo("    %s" % p, err=True)
            fail += 1
            continue
        jobs.append((pkg, specs[0]))

    total_names = len(jobs)
    click.echo("read %d specs" % total_names, err=True)

    def _load(item: tuple[str, Path]) -> tuple[str, Path, dict[str, Any] | None, str | None]:
        pkg, spec_path = item
        try:
            text = spec_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return pkg, spec_path, None, "cannot read %s (%s)" % (spec_path, exc)
        return pkg, spec_path, _nvchecker_resolve_entry(pkg, spec_path, text), None

    done = 0
    for pkg, spec_path, entry, err in _iter_bounded(8, _load, jobs, inflight=8):
        done += 1
        if bump_all and done % 500 == 0:
            _status_line("read %d/%d" % (done, total_names))
        if err:
            _status_nl()
            click.echo("%s  %s" % (pkg, err), err=True)
            fail += 1
            continue
        if not entry:
            nosource += 1
            fail += 1
            continue
        work.append((pkg, spec_path))
        entries[pkg] = entry
    _status_nl()
    if nosource:
        click.echo("%d specs have no http Source0/URL" % nosource, err=True)

    try:
        latest_map = _nvchecker_run_batched(entries) if entries else {}
    except click.ClickException as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    todo = len(work)
    workers = _host_workers("cpu")
    unpack_n = _host_workers("unpack")
    global _unpack_sem
    _unpack_sem = threading.Semaphore(unpack_n)
    click.echo(
        "bump workers %d  unpack %d  mem %dM" % (workers, unpack_n, _mem_available_mb()),
        err=True,
    )

    def _finish_item(item: tuple[str, Path]) -> tuple[str, str, str]:
        pkg, spec_path = item
        try:
            text = spec_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return pkg, "fail", "cannot read %s (%s)" % (spec_path, exc)
        latest = latest_map.get(pkg) or ""
        status, payload, old, new = _spec_bump_compute(pkg, spec_path, text, latest)
        if status != "ok":
            return pkg, status, payload
        new_text = payload
        macros = _spec_source_macros(new_text, new, pkg)
        patch_files = _spec_patch_files(new_text, macros, spec_path.parent)
        want_kernel_cfg = refresh_configs and (
            pkg == "kernel" or (spec_path.parent / "process_configs.sh").is_file()
        )
        real_patches = [
            (name, path)
            for name, path in patch_files
            if path.is_file() and not _patch_is_empty(path)
        ]
        want_tree = bool(
            (verify_patches and real_patches) or want_kernel_cfg
        )
        want_files = refresh_files and want_tree and bool(re.search(r"(?im)^%files\b", new_text))
        need_fetch = bool(verify_sources or refresh_hashes or want_tree)
        if need_fetch:
            pending = [
                (number, raw, expanded)
                for number, raw, expanded in _spec_http_sources(new_text, macros)
                if "%{" in expanded or "%(" in expanded
            ]
            if pending:
                bits = [
                    "Source%s has unexpandable macros: %s" % (number, raw)
                    for number, raw, expanded in pending
                ]
                return pkg, "fail", "; ".join(bits)
        archive_path: Path | None = None
        archive_digest: str | None = None
        if need_fetch:
            import httpx as _httpx

            def _do_fetch_and_tree() -> str | None:
                nonlocal new_text, archive_path, archive_digest
                with _httpx.Client(timeout=120.0) as web:
                    if verify_sources:
                        source_failures: list[str] = []
                        for number, raw, expanded in _spec_http_sources(new_text, macros):
                            if _verify_source_url(web, expanded):
                                continue
                            source_failures.append("Source%s %s" % (number, expanded))
                        if source_failures:
                            return "cannot fetch " + "; ".join(source_failures)
                    primaries = [
                        (number, expanded)
                        for number, _, expanded in _spec_http_sources(new_text, macros)
                        if number == "0"
                    ]
                    if (refresh_hashes or want_tree) and primaries:
                        got = _fetch_source_archive(web, primaries[0][1], None)
                        if not got:
                            return "Source0 download failed"
                        archive_path, archive_digest = got
                        if refresh_hashes:
                            new_text = _set_source_hash_guard(new_text, archive_digest)
                if not want_tree:
                    return None
                if archive_path is None:
                    return "no Source0 to unpack for patch check"
                big = False
                try:
                    big = archive_path.stat().st_size > 80 * 1024 * 1024
                except OSError:
                    big = True
                lock = _big_tree_lock if big else None
                if lock:
                    lock.acquire()
                try:
                    with tempfile.TemporaryDirectory() as raw:
                        tree = _unpack_source_archive(archive_path, Path(raw) / "src")
                        if tree is None:
                            return "cannot unpack Source0 for patch check"
                        errors, dropped = _check_or_apply_patches(
                            tree, real_patches, apply=want_kernel_cfg
                        )
                        if dropped:
                            new_text = _spec_drop_patches(new_text, dropped)
                        if errors:
                            return "patch " + "; ".join(errors)
                        if want_kernel_cfg:
                            cfg_err = _kernel_refresh_configs(spec_path.parent, tree, pkg)
                            if cfg_err:
                                return "configs %s" % cfg_err
                        if want_files:
                            new_text, _added = _bump_sync_files_from_tree(new_text, tree)
                            if archive_path is not None and not bump_all:
                                new_text, _added_i, _err = _bump_sync_files_via_install(
                                    spec_path, new_text, archive_path
                                )
                finally:
                    if lock:
                        lock.release()
                return None

            with _unpack_sem:
                err = _do_fetch_and_tree()
            if archive_path is not None:
                archive_path.unlink(missing_ok=True)
            if err:
                return pkg, "fail", err
        spec_path.write_text(new_text, encoding="utf-8")
        return pkg, "ok", "%s -> %s" % (old or "?", new)

    done_b = 0
    for pkg, status, msg in _iter_bounded(
        workers, _finish_item, work, inflight=workers
    ):
        done_b += 1
        if status == "ok":
            ok += 1
        elif status == "skip":
            skip += 1
        else:
            fail += 1
            _status_nl()
            click.echo("%s  %s" % (pkg, msg), err=True)
        _status_line("[%d/%d] %s" % (done_b, todo, status))
    _status_nl()
    click.echo("bumped=%s already=%s failed=%s" % (ok, skip, fail))
    if fail:
        sys.exit(1)

@cli.command("iso-dnf-log")
@click.argument(
    "log_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--spec-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Package trees root (default: fedora-rpms/oreon-11-rp1 under the rpm-specfiles repo).",
)
@click.option(
    "--release",
    "release_ref",
    default="oreon-11-rp1",
    help="Printed in the suggested orbscli build line.",
)
@click.option(
    "--print-providers",
    is_flag=True,
    help="List unique 'nothing provides …' strings from the log.",
)
def cmd_iso_dnf_log(
    log_path: Path,
    spec_root: Path | None,
    release_ref: str,
    print_providers: bool,
) -> None:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    names: set[str] = set()
    for m in re.finditer(r"\bneeded by (\S+) from\b", text):
        names.add(_nevra_to_name(m.group(1)))
    if print_providers:
        provs: set[str] = set()
        for m in re.finditer(r"\bnothing provides ([^\n]+?) needed by\b", text):
            provs.add(m.group(1).strip())
        if provs:
            click.echo("Missing providers (%d):" % len(provs))
            for p in sorted(provs):
                click.echo("  %s" % p)
            click.echo("")
    root = (
        spec_root.resolve()
        if spec_root is not None
        else (Path(__file__).resolve().parent.parent / "fedora-rpms" / "oreon-11-rp1")
    )
    have: list[str] = []
    seen: set[str] = set()
    for n in sorted(names):
        d = _spec_dir_for_rpm(root, n)
        if d and d not in seen:
            seen.add(d)
            have.append(d)
    click.echo("RPM names referenced in log: %d" % len(names))
    if have:
        click.echo("Local spec dirs (%d): %s" % (len(have), " ".join(have)))
        click.echo("orbscli build --release %s %s" % (release_ref, " ".join(have)))
    else:
        click.echo("No matching package dirs under %s" % root)
    uncovered = sorted(n for n in names if _spec_dir_for_rpm(root, n) is None)
    if uncovered:
        lim = 100
        tail = uncovered[lim:]
        head = uncovered[:lim]
        click.echo("No local spec dir for (%d): %s" % (len(uncovered), " ".join(head)))
        if tail:
            click.echo("  … (%d more)" % len(tail))

_RE_LOG_PKG_NAME = re.compile(
    r"^(?P<pkg>.+?)-(?:x86_64|aarch64|noarch|i686|ppc64le|s390x)-job\d+\.log$"
)
_RE_NO_MATCH_PKG = re.compile(r"No matching package to install:\s*'([^']+)'")
_RE_UNPACKAGED_MARKER = re.compile(r"Installed \(but unpackaged\) file\(s\) found:")
_RE_PATH_LINE = re.compile(r"^(/\S+)\s*$")
_RE_FILES_SECTION = re.compile(r"(?im)^%files(?:\s+[^\n]*)?$")
_RE_BUILDREQUIRES_LINE = re.compile(r"(?im)^BuildRequires:.*$")
_RE_SPEC_SECTION = re.compile(r"(?im)^%(?:files|changelog|description|prep|build|install|check|clean|pre|post|preun|postun)\b")


def _fastfix_pkg_name(log_path: Path) -> str | None:
    """Derive the package name from a ``<pkg>-<arch>-job<id>.log`` filename."""
    match = _RE_LOG_PKG_NAME.match(log_path.name)
    return match.group("pkg") if match else None


def _fastfix_unpackaged_files(log_text: str) -> list[str]:
    """Collect installed-but-unpackaged paths listed around the rpm marker."""
    lines = log_text.splitlines()
    found: list[str] = []
    for index, line in enumerate(lines):
        if not _RE_UNPACKAGED_MARKER.search(line):
            continue
        for probe in lines[index + 1 : index + 201]:
            match = _RE_PATH_LINE.match(probe.strip())
            if match:
                candidate = match.group(1)
                if candidate not in found:
                    found.append(candidate)
                continue
            if probe.strip():
                break
    return found


def _fastfix_cmake_missing_python3(log_text: str) -> bool:
    """Detect the deterministic ``Could NOT find Python3`` devel failure."""
    return bool(
        re.search(
            r"Could NOT find Python3 \(missing:[\s\S]*?Development",
            log_text,
        )
    )


def _fastfix_no_match_packages(log_text: str) -> list[str]:
    """Return ``No matching package to install`` payloads, de-duplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _RE_NO_MATCH_PKG.finditer(log_text):
        item = match.group(1).strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _fastfix_is_plain_rpm_name(dep: str) -> bool:
    """Return whether a missing payload is a plain package name (not a capability)."""
    return bool(re.fullmatch(r"[A-Za-z0-9_+.\-]+", dep))


_RE_CMAKE_MISSING_CONFIG = re.compile(
    r'Could not find a package configuration file provided by\s+"([^"]+)"',
    re.IGNORECASE,
)
_RE_CMAKE_MISSING_MODULE = re.compile(
    r'By not providing "Find([^"]+)\.cmake" in CMAKE_MODULE_PATH', re.IGNORECASE
)
_RE_CMAKE_MISSING_CONFIG_ALT = re.compile(
    r'Could not find a configuration file for package "([^"]+)"', re.IGNORECASE
)
_RE_CMAKE_VERSION_SKEW = re.compile(
    r"Could NOT find (\S+) \(found version \"([^\"]+)\"[^)]*\)", re.IGNORECASE
)
_RE_CMAKE_REQUESTED_VERSION = re.compile(
    r'\(requested version ([0-9][0-9A-Za-z.]*)\)', re.IGNORECASE
)


def _fastfix_missing_cmake_configs(log_text: str) -> list[str]:
    """Return CMake config/module names the build could not find, de-duplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _RE_CMAKE_MISSING_CONFIG.finditer(log_text):
        item = match.group(1).strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    for match in _RE_CMAKE_MISSING_MODULE.finditer(log_text):
        item = match.group(1).strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    for match in _RE_CMAKE_MISSING_CONFIG_ALT.finditer(log_text):
        item = match.group(1).strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _fastfix_cmake_version_skews(log_text: str) -> list[str]:
    """Return ``Found X but requested Y`` config version skews, de-duplicated."""
    requested = _RE_CMAKE_REQUESTED_VERSION.search(log_text)
    wanted = requested.group(1) if requested else None
    seen: set[str] = set()
    out: list[str] = []
    for match in _RE_CMAKE_VERSION_SKEW.finditer(log_text):
        item = "%s found %s%s" % (
            match.group(1).strip(),
            match.group(2).strip(),
            (" wants %s" % wanted) if wanted else "",
        )
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _fastfix_config_provider_guess(config: str) -> str | None:
    """Guess the in-stack provider package for a CMake config name."""
    normalized = re.sub(r"[^a-z0-9]", "", config.lower())
    for prefix, pkg_prefix in (("kf6", "kf6-"), ("kf5", "kf5-"), ("qt6", "qt6-"), ("qt5", "qt5-")):
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            return pkg_prefix + normalized[len(prefix):]
    return None


def _fastfix_patch_failure(log_text: str) -> bool:
    """Detect %prep patch application failures (manual review required)."""
    return bool(
        re.search(
            r"hunk FAILED|can'?t find file to patch|Patch .* failed|Bad exit status from \S+ \(%prep\)",
            log_text,
            re.IGNORECASE,
        )
    )


def _fastfix_source_hash_failure(log_text: str) -> bool:
    """Detect source checksum verification failures (manual review required)."""
    return bool(
        re.search(r"hash mismatch|Bad source|does not match expected", log_text, re.IGNORECASE)
    )


def _files_path_macros(spec_text: str = "") -> dict[str, str]:
    macros = {
        "_firmwarepath": "/usr/lib/firmware",
        "_bindir": "/usr/bin",
        "_sbindir": "/usr/sbin",
        "_libexecdir": "/usr/libexec",
        "_datadir": "/usr/share",
        "_sysconfdir": "/etc",
        "_mandir": "/usr/share/man",
        "_includedir": "/usr/include",
        "_docdir": "/usr/share/doc",
        "_unitdir": "/usr/lib/systemd/system",
        "_userunitdir": "/usr/lib/systemd/user",
        "_prefix": "/usr",
        "_libdir": "/usr/lib64",
    }
    try:
        proc = subprocess.run(
            [
                "rpm",
                "--eval",
                "%{_libdir} %{_libexecdir} %{_datadir} %{_bindir} %{_mandir} %{_includedir}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parts = proc.stdout.split()
            keys = ["_libdir", "_libexecdir", "_datadir", "_bindir", "_mandir", "_includedir"]
            for key, val in zip(keys, parts):
                if val.startswith("/"):
                    macros[key] = val
    except (OSError, subprocess.TimeoutExpired):
        pass
    for name, value in re.findall(
        r"(?im)^%(?:global|define)\s+(_[A-Za-z0-9]+)\s+(\S+)",
        spec_text or "",
    ):
        if value.startswith("/") or value.startswith("%"):
            macros[name] = value.strip()
    # expand macros that still reference other macros once
    for _ in range(4):
        changed = False
        for name, value in list(macros.items()):
            if "%{" not in value:
                continue
            nxt = re.sub(
                r"%\{([A-Za-z0-9_]+)\}",
                lambda m: macros.get(m.group(1), m.group(0)),
                value,
            )
            if nxt != value and "%{" not in nxt:
                macros[name] = nxt
                changed = True
        if not changed:
            break
    return macros


def _files_macroize_abs(abspath: str, spec_text: str) -> str:
    path = (abspath or "").strip()
    if not path:
        return path
    macros = _files_path_macros(spec_text)
    best_name = None
    best_val = ""
    for name, value in macros.items():
        val = (value or "").rstrip("/")
        if not val.startswith("/"):
            continue
        if path == val or path.startswith(val + "/"):
            if len(val) > len(best_val):
                best_name, best_val = name, val
    if not best_name:
        return path
    rest = path[len(best_val) :]
    return "%{" + best_name + "}" + rest


def _files_has_generated_list(spec_text: str) -> bool:
    return bool(re.search(r"(?im)^%files\b[^\n]*\s-f\s+\S+", spec_text or ""))


def _files_sed_exclude_rels(spec_text: str) -> list[str]:
    """Relative paths yanked from a generated file list via sed '/^path/d'."""
    out: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"""/\^((?:[^/\n\\]|\\/)+?)/d['"]?""", spec_text or ""):
        rel = match.group(1).replace("\\/", "/").strip("/")
        if rel and rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def _files_install_roots(spec_text: str) -> list[str]:
    """Absolute roots sed file lists are relative to (from pushd / FIRMWAREDIR)."""
    macros = _files_path_macros(spec_text)
    roots: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        path = _files_expand_entry(path, macros).rstrip("/")
        if path.startswith("/") and path not in seen:
            seen.add(path)
            roots.append(path)

    for match in re.finditer(
        r"(?im)pushd\s+%\{buildroot\}/?(%\{?[A-Za-z0-9_]+\}?|\S+)",
        spec_text or "",
    ):
        add(match.group(1))
    for match in re.finditer(
        r"(?im)(?:FIRMWAREDIR|DESTDIR)=(%\{_firmwarepath\}|/usr/lib/firmware)",
        spec_text or "",
    ):
        add(match.group(1))
    if "%{_firmwarepath}" in (spec_text or ""):
        add("%{_firmwarepath}")
    if not roots:
        add("/usr")
    return roots


def _files_collect_patterns(spec_text: str) -> list[str]:
    macros = _files_path_macros(spec_text)
    patterns: list[str] = []
    for _section, start, end in _files_section_spans(spec_text):
        for entry in _files_section_entries(spec_text[start:end]):
            expanded = _files_expand_entry(entry, macros)
            if expanded:
                patterns.append(expanded)
    return patterns


def _files_find_in_tree(tree: Path, rel: str) -> Path | None:
    rel = (rel or "").strip().strip("/")
    if not rel or not tree.is_dir():
        return None
    direct = tree / rel
    if direct.is_dir():
        return direct
    kids = [p for p in tree.iterdir() if p.is_dir()]
    if len(kids) == 1 and (kids[0] / rel).is_dir():
        return kids[0] / rel
    for kid in kids:
        cand = kid / rel
        if cand.is_dir():
            return cand
    return None


def _files_path_covered(patterns: list[str], abspath: str) -> bool:
    path = abspath.rstrip("/")
    return any(
        _files_entry_covers(pat, path) or _files_entry_covers(pat, path + "/")
        for pat in patterns
    )


def _files_enumerated_parents(
    spec_text: str,
) -> dict[str, list[str]]:
    """parent_abs -> explicit child basenames listed as dirs in the same %files section."""
    macros = _files_path_macros(spec_text)
    out: dict[str, list[str]] = {}
    for _section, start, end in _files_section_spans(spec_text):
        parents: dict[str, set[str]] = {}
        for entry in _files_section_entries(spec_text[start:end]):
            expanded = _files_expand_entry(entry, macros)
            if not expanded or expanded.startswith("%dir "):
                continue
            raw = expanded
            if any(ch in raw for ch in "*?["):
                base = raw.rstrip("/")
                parent = base.rsplit("/", 1)[0] if "/" in base else ""
                if parent:
                    parents.setdefault(parent, set())
                continue
            if not (raw.endswith("/") or raw.count("/") >= 3):
                continue
            path = raw.rstrip("/")
            if "/" not in path:
                continue
            parent, child = path.rsplit("/", 1)
            if any(ch in child for ch in "*?["):
                continue
            parents.setdefault(parent, set()).add(child)
        for parent, kids in parents.items():
            if len(kids) >= 2:
                out.setdefault(parent, [])
                for kid in sorted(kids):
                    if kid not in out[parent]:
                        out[parent].append(kid)
    return out


def _files_spec_build_trivial(spec_text: str) -> bool:
    m = re.search(
        r"(?ims)^%build\b(.*?)(?=^%(?:install|check|files|changelog|package|prep|clean)\b)",
        spec_text or "",
    )
    if not m:
        return True
    body = m.group(1)
    body = re.sub(r"(?m)^\s*#.*$", "", body)
    body = re.sub(r"(?m)^\s*%\{\?.*\}$", "", body)
    return not body.strip()


def _files_spec_is_noarch(spec_text: str) -> bool:
    return bool(re.search(r"(?im)^BuildArch\s*:\s*noarch\b", spec_text or ""))


def _files_expand_entry(entry: str, macros: dict[str, str] | None = None) -> str:
    out = (entry or "").strip()
    dir_only = False
    if out.startswith("%dir"):
        dir_only = True
        out = out[4:].strip()
    if not out:
        return ""
    if out.startswith(("%license", "%doc", "%config", "%attr", "%verify", "%ghost", "%defattr")):
        return ""
    macros = macros or _files_path_macros()
    for _ in range(8):
        nxt = re.sub(
            r"%\{([A-Za-z0-9_]+)\}",
            lambda m: macros.get(m.group(1), m.group(0)),
            out,
        )
        if nxt == out:
            break
        out = nxt
    if "%{" in out:
        return ""
    return ("%dir " + out) if dir_only else out


def _files_entry_covers(expanded_entry: str, abspath: str) -> bool:
    from fnmatch import fnmatch

    raw = (expanded_entry or "").rstrip()
    path = (abspath or "").rstrip()
    if not raw or not path:
        return False
    dir_only = False
    if raw.startswith("%dir "):
        dir_only = True
        raw = raw[5:].strip()
    pat = raw
    if dir_only:
        # %dir only owns the directory node, not its children
        return path == pat.rstrip("/")
    if any(ch in pat for ch in "*?["):
        base = pat.rstrip("/")
        if fnmatch(path, base) or fnmatch(path, base + "/*"):
            return True
        parent = path
        while parent.startswith("/"):
            if fnmatch(parent, base):
                return True
            if parent == "/" or parent.count("/") < 2:
                break
            parent = parent.rsplit("/", 1)[0]
        return False
    if pat.endswith("/"):
        return path == pat.rstrip("/") or path.startswith(pat)
    return path == pat or path.startswith(pat.rstrip("/") + "/")


def _files_section_spans(spec_text: str) -> list[tuple[re.Match[str], int, int]]:
    spans: list[tuple[re.Match[str], int, int]] = []
    sections = list(_RE_FILES_SECTION.finditer(spec_text))
    for idx, section in enumerate(sections):
        start = section.end()
        end = sections[idx + 1].start() if idx + 1 < len(sections) else len(spec_text)
        nxt = _RE_SPEC_SECTION.search(spec_text, start)
        if nxt and nxt.start() < end and not nxt.group(0).lower().startswith("%files"):
            end = nxt.start()
        # changelog ends files
        clog = re.search(r"(?im)^%changelog\b", spec_text[start:end])
        if clog:
            end = start + clog.start()
        spans.append((section, start, end))
    return spans


def _files_section_entries(body: str) -> list[str]:
    out: list[str] = []
    for line in body.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith(("%license", "%doc", "%config", "%attr", "%verify", "%ghost", "%defattr", "%files")):
            continue
        if raw.startswith("%dir"):
            rest = raw[4:].strip()
            if rest:
                out.append("%dir " + rest)
            continue
        if raw.startswith("%") and not raw.startswith("%{"):
            continue
        out.append(raw)
    return out


def _files_collapse_unpackaged(paths: list[str], spec_text: str) -> list[str]:
    """Turn loose unpackaged paths into compact %files entries (prefer dir + macros)."""
    macros = _files_path_macros(spec_text)
    fw = macros.get("_firmwarepath", "/usr/lib/firmware").rstrip("/")
    cleaned = [
        p
        for p in paths
        if p
        and not p.startswith(("/usr/lib/debug", "/usr/lib/.build-id", "/usr/src/debug"))
    ]
    if not cleaned:
        return []

    # group by parent dir when many files share one
    by_parent: dict[str, list[str]] = {}
    for path in cleaned:
        if path.endswith("/"):
            parent = path.rstrip("/")
            by_parent.setdefault(parent, []).append(path)
            continue
        parent = path.rsplit("/", 1)[0] if "/" in path else path
        by_parent.setdefault(parent, []).append(path)

    entries: list[str] = []
    seen: set[str] = set()
    for parent, kids in sorted(by_parent.items()):
        if any(k.endswith("/") for k in kids) or len(kids) >= 2 or parent.startswith(fw + "/"):
            cand = parent + "/"
        else:
            cand = kids[0]
        cand = _files_macroize_abs(cand, spec_text)
        if cand not in seen:
            seen.add(cand)
            entries.append(cand)
    return entries


def _files_entry_already_present(spec_text: str, entry: str) -> bool:
    needle = entry.strip()
    if not needle:
        return True
    if needle in spec_text:
        return True
    macros = _files_path_macros(spec_text)
    expanded = _files_expand_entry(needle, macros)
    if not expanded:
        return False
    for _section, start, end in _files_section_spans(spec_text):
        for existing in _files_section_entries(spec_text[start:end]):
            if _files_expand_entry(existing, macros) == expanded:
                return True
            if _files_entry_covers(_files_expand_entry(existing, macros), expanded.rstrip("/")):
                return True
    return False


def _files_pick_section(spec_text: str, entries: list[str]) -> tuple[re.Match[str], int, int] | None:
    spans = _files_section_spans(spec_text)
    if not spans:
        return None
    macros = _files_path_macros(spec_text)
    expanded_new = [_files_expand_entry(e, macros) for e in entries]
    expanded_new = [e for e in expanded_new if e]

    def score(span: tuple[re.Match[str], int, int]) -> int:
        _section, start, end = span
        body = spec_text[start:end]
        pts = 0
        for existing in _files_section_entries(body):
            ex = _files_expand_entry(existing, macros)
            if not ex:
                continue
            for cand in expanded_new:
                # common directory prefix length
                a = ex.rstrip("/")
                b = cand.rstrip("/")
                while a and b and not (b.startswith(a + "/") or b == a or a.startswith(b + "/")):
                    a = a.rsplit("/", 1)[0] if "/" in a else ""
                if a and (b == a or b.startswith(a + "/")):
                    pts += a.count("/")
        # prefer named subpackages that already mention the vendor path
        header = _section.group(0).lower()
        for cand in expanded_new:
            parts = [p for p in cand.strip("/").split("/") if p and p not in {"usr", "lib", "lib64", "share"}]
            for part in parts[-3:]:
                if part.lower() in header:
                    pts += 3
        if "wwan" in header or "accel" in header:
            pts -= 2
        return pts

    best = max(spans, key=score)
    return best


def _fastfix_apply_unpackaged(spec_text: str, files: list[str]) -> tuple[str, list[str]]:
    """Append unpackaged files to the best-matching ``%files`` section."""
    entries = _files_collapse_unpackaged(files, spec_text)
    todo = [e for e in entries if not _files_entry_already_present(spec_text, e)]
    if not todo:
        return spec_text, []
    picked = _files_pick_section(spec_text, todo)
    if not picked:
        return spec_text, []
    _section, _start, end = picked
    insertion = "".join(path + "\n" for path in todo)
    return spec_text[:end].rstrip("\n") + "\n" + insertion + spec_text[end:], todo


def _bump_sync_files_from_tree(spec_text: str, tree: Path) -> tuple[str, list[str]]:
    """Add missing %files dir entries for new Source0 paths every package layout."""
    if not re.search(r"(?im)^%files\b", spec_text or ""):
        return spec_text, []
    patterns = _files_collect_patterns(spec_text)
    missing_abs: list[str] = []
    seen: set[str] = set()

    def consider(abspath: str) -> None:
        path = abspath if abspath.endswith("/") else abspath + "/"
        key = path.rstrip("/")
        if key in seen:
            return
        if _files_path_covered(patterns, key):
            return
        seen.add(key)
        missing_abs.append(path)

    # 1) sed-excluded prefixes from generated file lists (linux-firmware style and friends)
    excludes = _files_sed_exclude_rels(spec_text)
    roots = _files_install_roots(spec_text) if excludes else []
    for rel in excludes:
        src = _files_find_in_tree(tree, rel)
        if src is None:
            continue
        for root in roots:
            base = "%s/%s" % (root.rstrip("/"), rel.strip("/"))
            # if exclude is a single path component fully removed, every child dir must be packaged
            if "/" not in rel.strip("/"):
                if not src.is_dir():
                    continue
                for child in sorted(src.iterdir()):
                    if child.is_dir():
                        consider("%s/%s/" % (base, child.name))
                    elif child.is_file():
                        consider("%s/%s" % (base, child.name))
            else:
                # nested exclude like intel/avs: the excluded tree itself must be covered
                consider(base + "/")

    # 2) for specs without generated -f lists, fill sibling dirs under enumerated parents
    if not _files_has_generated_list(spec_text):
        for parent, _kids in _files_enumerated_parents(spec_text).items():
            rel = None
            for root in _files_install_roots(spec_text) + [
                _files_path_macros(spec_text).get(k, "")
                for k in (
                    "_firmwarepath",
                    "_datadir",
                    "_libdir",
                    "_libexecdir",
                    "_includedir",
                    "_mandir",
                    "_prefix",
                )
            ]:
                root = (root or "").rstrip("/")
                if root and (parent == root or parent.startswith(root + "/")):
                    rel = parent[len(root) :].lstrip("/")
                    break
            src = None
            if rel is not None:
                src = _files_find_in_tree(tree, rel)
            if src is None:
                parts = parent.strip("/").split("/")
                for n in (3, 2, 1):
                    if len(parts) >= n:
                        cand = _files_find_in_tree(tree, "/".join(parts[-n:]))
                        if cand is not None:
                            src = cand
                            break
            if src is None or not src.is_dir():
                continue
            for child in sorted(src.iterdir()):
                if child.is_dir():
                    consider("%s/%s/" % (parent.rstrip("/"), child.name))

    if not missing_abs:
        return spec_text, []
    # prefer macroized paths in the apply path via collapse
    return _fastfix_apply_unpackaged(spec_text, missing_abs)


def _bump_sync_firmware_files(spec_text: str, tree: Path) -> tuple[str, list[str]]:
    return _bump_sync_files_from_tree(spec_text, tree)


def _bump_sync_files_via_install(
    spec_path: Path,
    spec_text: str,
    archive_path: Path,
) -> tuple[str, list[str], str | None]:
    """Run a local noarch rpmbuild install and fold unpackaged paths into %files."""
    if not _files_spec_is_noarch(spec_text) or not _files_spec_build_trivial(spec_text):
        return spec_text, [], None
    if not shutil.which("rpmbuild"):
        return spec_text, [], None
    with tempfile.TemporaryDirectory(prefix="orbscli-files-") as raw:
        top = Path(raw)
        for name in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"):
            (top / name).mkdir()
        shutil.copy2(archive_path, top / "SOURCES" / archive_path.name)
        # copy local non-URL sources/patches sitting next to the spec
        for src in spec_path.parent.iterdir():
            if not src.is_file():
                continue
            if src.suffix.lower() in {".spec", ".md", ".txt"} and src.name != spec_path.name:
                continue
            if src.name == archive_path.name:
                continue
            if src.name.endswith((".tar", ".tar.gz", ".tar.xz", ".tar.bz2", ".tgz", ".zip", ".crate")):
                # only auto-copy if referenced by the spec basename
                if src.name not in spec_text and src.name not in archive_path.name:
                    continue
            dest = top / "SOURCES" / src.name
            if not dest.exists():
                try:
                    shutil.copy2(src, dest)
                except OSError:
                    pass
        spec_dest = top / "SPECS" / spec_path.name
        spec_dest.write_text(spec_text, encoding="utf-8")
        cmd = [
            "rpmbuild",
            "-bi",
            "--nodeps",
            "--define",
            "_topdir %s" % top,
            "--define",
            "oreon 11",
            str(spec_dest),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
            )
        except subprocess.TimeoutExpired:
            return spec_text, [], "rpmbuild install timed out"
        log = "%s\n%s" % (proc.stdout or "", proc.stderr or "")
        unpackaged = _fastfix_unpackaged_files(log)
        if not unpackaged:
            if proc.returncode != 0:
                return spec_text, [], None
            return spec_text, [], None
        new_text, added = _fastfix_apply_unpackaged(spec_text, unpackaged)
        return new_text, added, None


def _fastfix_apply_buildrequires(spec_text: str, deps: list[str]) -> tuple[str, list[str]]:
    """Insert missing plain ``BuildRequires`` after the last such line."""
    missing = [
        dep
        for dep in deps
        if not re.search(
            r"(?im)^BuildRequires:\s*%s\s*$" % re.escape(dep), spec_text
        )
    ]
    if not missing:
        return spec_text, []
    anchors = list(_RE_BUILDREQUIRES_LINE.finditer(spec_text))
    lines = "".join("BuildRequires:\t%s\n" % dep for dep in missing)
    if anchors:
        at = anchors[-1].end()
        return spec_text[:at] + "\n" + lines.rstrip("\n") + spec_text[at:], missing
    anchor = re.search(r"(?im)^Name\s*:.*\n", spec_text)
    if anchor:
        at = anchor.end()
        return spec_text[:at] + lines + spec_text[at:], missing
    return lines + spec_text, missing


def _chain_pkg_count(text: str) -> int:
    return len([p for p in (text or "").replace(":", " ").split() if p])


def _write_chain_outputs(
    out_dir: Path, release_ref: str, oreon: str, f44: str
) -> tuple[Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", release_ref).strip("-") or "chain"
    oreon_path = out_dir / ("chain-%s-oreon-11.txt" % slug)
    f44_path = out_dir / ("chain-%s-fedora-44.txt" % slug)
    layers_path = out_dir / ("chain-%s-oreon-11.layers" % slug)
    oreon_path.write_text((oreon or "").strip() + "\n", encoding="utf-8")
    f44_path.write_text((f44 or "").strip() + "\n", encoding="utf-8")
    layers = [part.strip() for part in (oreon or "").split(":") if part.strip()]
    body = "".join("%d\t%s\n" % (i, layer) for i, layer in enumerate(layers, 1))
    layers_path.write_text(body, encoding="utf-8")
    return oreon_path, f44_path, layers_path


@cli.command("generate-chain")
@click.option("--release", "release_ref", required=True, help="Oreon release directory/name, e.g. oreon-11-rp1")
@click.option("--pushed", is_flag=True, help="Use specs changed in origin/HEAD...HEAD instead of the working tree")
@click.option("--spec-root", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--packages", "packages_blob", default=None, help="Raw package names (whitespace, commas, or newlines)")
@click.option("--packages-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="File containing one or more raw package names")
@click.option("--side-tag", "side_tag_ref", default=None, help="Side tag id or name whose dnf repo also counts as satisfied")
@click.option(
    "--arch",
    "repo_arch",
    default="x86_64,aarch64",
    help="Repo arch dirs. Comma list. Each arch uses rpmspec --target against that arch dnf repo.",
)
@click.option(
    "--repo-base",
    envvar="ORBSCLI_REPO_BASE",
    default="https://repo-us.oreonhq.com",
    help="dnf repo root (release/tag/arch)",
)
@click.option("--no-repo-filter", is_flag=True, help="Do not drop packages already in testing or --side-tag")
@click.option(
    "--stale",
    is_flag=True,
    default=False,
    help="Chain every distgit spec whose Version does not match testing or --side-tag.",
)
@click.option(
    "--out-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Where to write chain files. Default is the rpm-specfiles repo root.",
)
def cmd_generate_chain(
    release_ref: str,
    pushed: bool,
    spec_root: Path | None,
    packages_blob: str | None,
    packages_file: Path | None,
    side_tag_ref: str | None,
    repo_arch: str,
    repo_base: str,
    no_repo_filter: bool,
    stale: bool,
    out_dir: Path | None,
) -> None:
    """Order missing packages into dependency layers."""
    repo_root = Path(__file__).resolve().parent.parent
    root = (spec_root.expanduser().resolve() if spec_root else repo_root / "fedora-rpms" / release_ref)
    if not root.is_dir():
        raise click.ClickException("spec root is not a directory: %s" % root)
    git_root = repo_root
    try:
        import subprocess
        found_root = subprocess.run(
            ("git", "rev-parse", "--show-toplevel"), cwd=root, check=True,
            capture_output=True, text=True
        ).stdout.strip()
        if found_root:
            git_root = Path(found_root).resolve()
    except (OSError, subprocess.CalledProcessError):
        pass
    if stale and no_repo_filter:
        raise click.UsageError("--stale needs repo metadata, do not pass --no-repo-filter")
    if stale and (packages_blob is not None or packages_file is not None):
        raise click.UsageError("--stale cannot be combined with --packages")
    if packages_blob is not None and packages_file is not None:
        raise click.UsageError("use only one of --packages and --packages-file")
    raw_names: list[str] = []
    if not stale:
        if packages_file is not None:
            raw_names = parse_package_blob(packages_file.read_text(encoding="utf-8"))
        elif packages_blob is not None:
            raw_names = parse_package_blob(packages_blob)
    if raw_names:
        changed = []
        for name in raw_names:
            hits = find_pkg_specs(root, name)
            if len(hits) == 1:
                changed.append(hits[0])
            else:
                click.echo("%s: no unique spec" % name, err=True)
    elif stale:
        changed = []
    else:
        changed = _git_changed_spec_paths(git_root, root, pushed)
    if not stale and not changed:
        click.echo("no changed specs found")
        return
    present: set[str] | dict[str, set[str]] | None = None
    urls_by_arch: dict[str, list[str]] = {}
    if not no_repo_filter:
        tags = ["testing"]
        side_dirs: list[str] = []
        if side_tag_ref:
            side_dirs = _side_tag_repo_dirs(side_tag_ref)
            for name in side_dirs:
                if name not in tags:
                    tags.append(name)
        present: dict[str, set[str]] = {}
        arches = _parse_repo_arches(repo_arch)
        for arch in arches:
            urls: list[str] = []
            seen_url: set[str] = set()
            got_side = False
            for tag in tags:
                url = _repo_url(repo_base, release_ref, tag, arch)
                if url in seen_url:
                    continue
                if not _repo_repomd_ok(url):
                    if tag == "testing":
                        raise click.ClickException("no dnf repo at %s" % url)
                    continue
                seen_url.add(url)
                urls.append(url)
                if tag in side_dirs:
                    got_side = True
            if side_dirs and not got_side:
                raise click.ClickException(
                    "no dnf repo for side tag %s on %s" % (side_tag_ref, arch)
                )
            if not urls:
                raise click.ClickException("no dnf repo for %s" % arch)
            click.echo("querying dnf %s" % arch, err=True)
            provides, requires, provides_evr, srpms = _dnf_repo_maps(
                urls, arch, with_solver=False
            )
            click.echo(
                "dnf %s %d rpms %d srpms %d caps"
                % (arch, len(requires), len(srpms), len(provides)),
                err=True,
            )
            if len(requires) < _MIN_REPO_RPMS:
                click.echo(
                    "skipping %s, dnf only saw %d rpms" % (arch, len(requires)),
                    err=True,
                )
                continue
            caps = set(provides)
            present[arch] = _RepoDetail(caps, provides, requires, provides_evr, srpms=srpms)
            urls_by_arch[arch] = urls
        if not present:
            raise click.ClickException("no usable dnf repos")
    if stale:
        seeds = _stale_distgit_seeds(root, present)
        click.echo("stale=%d" % len(seeds), err=True)
        if not seeds:
            click.echo("distgit versions already match the repo")
            return
    else:
        seeds = _chain_seed_names(changed)
    click.echo("seeds=%d" % len(seeds), err=True)
    global _RPMSPEC_FAILS
    _RPMSPEC_FAILS = 0
    host_arch = os.uname().machine
    host_urls = urls_by_arch.get(host_arch) or next(iter(urls_by_arch.values()), [])
    chain, selected, f44_chain = _generate_chain_from_specs(
        root, seeds, present, repo_urls=host_urls, repo_arch=host_arch if host_urls else None
    )
    if any(name in {"gcc", "gcc-c++"} for name in selected):
        click.echo("GCC is in this chain", err=True)
    dest = out_dir.expanduser().resolve() if out_dir else repo_root
    oreon_path, f44_path, layers_path = _write_chain_outputs(
        dest, release_ref, chain, f44_chain or ""
    )
    n_oreon = _chain_pkg_count(chain)
    n_f44 = _chain_pkg_count(f44_chain or "")
    n_layers = len([p for p in (chain or "").split(":") if p.strip()])
    click.echo("fedora-44 %d pkgs  %s" % (n_f44, f44_path), err=True)
    click.echo("oreon-11 %d pkgs %d layers  %s" % (n_oreon, n_layers, oreon_path), err=True)
    click.echo("layers  %s" % layers_path, err=True)
    preview = 60
    if n_oreon <= preview and sys.stdout.isatty():
        click.echo("fedora-44 %s" % (f44_chain or "").strip())
        click.echo("oreon-11 %s" % chain)
    elif not sys.stdout.isatty():
        click.echo("oreon-11 %s" % chain)
    click.echo(
        "packages=%d source=%s rpmspec-fail=%d"
        % (len(selected), "stale" if stale else ("pushed" if pushed else "unpushed"), _RPMSPEC_FAILS),
        err=True,
    )


@cli.command("fast-fix")
@click.option(
    "--log-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Directory of build logs (see: logs chain-fail --out).",
)
@click.option(
    "-d",
    "--spec-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="rpm-specfiles tree (default: fedora-rpms/oreon-11-rp1 next to the repo).",
)
@click.option(
    "-p",
    "--packages",
    "packages_blob",
    default=None,
    help="Only fix these packages (spaces, commas, or newlines).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what would change without writing specs.",
)
def cmd_fast_fix(
    log_dir: Path, spec_root: Path | None, packages_blob: str | None, dry_run: bool
) -> None:
    """Auto-fix deterministic build failures from downloaded build logs.

    Applies only safe, mechanical repairs: appending installed-but-unpackaged
    files to ``%files``, adding ``BuildRequires: python3-devel`` for the
    ``Could NOT find Python3`` CMake failure, and adding plain missing
    ``BuildRequires`` names. Dependency-chain problems (``cmake(...)`` /
    ``pkgconfig(...)`` capabilities, ``nothing provides``) and ``%prep``
    patch or checksum failures are reported for manual handling.
    """
    root = (
        spec_root.expanduser().resolve()
        if spec_root is not None
        else (Path(__file__).resolve().parent.parent / "fedora-rpms" / "oreon-11-rp1")
    )
    if not root.is_dir():
        click.echo("not a directory  %s" % root, err=True)
        sys.exit(2)
    only = set(parse_package_blob(packages_blob)) if packages_blob else None
    logs = sorted(log_dir.glob("*.log"))
    if not logs:
        click.echo("no .log files under %s" % log_dir, err=True)
        sys.exit(2)
    by_package: dict[str, list[Path]] = {}
    for log_path in logs:
        pkg = _fastfix_pkg_name(log_path)
        if pkg is None or (only is not None and pkg not in only):
            continue
        by_package.setdefault(pkg, []).append(log_path)
    if not by_package:
        click.echo("no logs matched known package names", err=True)
        sys.exit(2)
    fixed = 0
    need_chain: dict[str, list[str]] = {}
    need_manual: dict[str, list[str]] = {}
    for pkg in sorted(by_package):
        specs = find_pkg_specs(root, pkg)
        if len(specs) != 1:
            need_manual.setdefault(pkg, []).append(
                "no unique spec under %s" % root if not specs else "multiple specs, refine --spec-root"
            )
            continue
        spec_path = specs[0]
        try:
            spec_text = spec_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            need_manual.setdefault(pkg, []).append("cannot read spec (%s)" % exc)
            continue
        combined = "\n".join(
            log.read_text(encoding="utf-8", errors="replace") for log in by_package[pkg]
        )
        actions: list[str] = []
        unpackaged = _fastfix_unpackaged_files(combined)
        if unpackaged:
            spec_text, added = _fastfix_apply_unpackaged(spec_text, unpackaged)
            if added:
                actions.append("packaged %d file(s): %s" % (len(added), ", ".join(added[:5])))
            skipped = [path for path in unpackaged if path not in spec_text]
            if skipped:
                need_manual.setdefault(pkg, []).append(
                    "unpackaged debug paths need review: %s" % ", ".join(skipped[:5])
                )
        if _fastfix_cmake_missing_python3(combined):
            spec_text, added = _fastfix_apply_buildrequires(spec_text, ["python3-devel"])
            if added:
                actions.append("added BuildRequires: python3-devel")
        plain_missing = sorted(
            {
                dep
                for dep in _fastfix_no_match_packages(combined)
                if _fastfix_is_plain_rpm_name(dep)
            }
        )
        if plain_missing:
            spec_text, added = _fastfix_apply_buildrequires(spec_text, plain_missing)
            if added:
                actions.append("added BuildRequires: %s" % ", ".join(added))
        capabilities = sorted(
            {
                dep
                for dep in _fastfix_no_match_packages(combined)
                if not _fastfix_is_plain_rpm_name(dep)
            }
        )
        if capabilities:
            need_chain.setdefault(pkg, []).extend(
                "build provider for %s first" % dep for dep in capabilities
            )
        for skew in _fastfix_cmake_version_skews(combined):
            need_chain.setdefault(pkg, []).append(
                "CMake version skew %s: rebuild providers at one version" % skew
            )
        for config in _fastfix_missing_cmake_configs(combined):
            guess = _fastfix_config_provider_guess(config)
            if guess:
                need_chain.setdefault(pkg, []).append(
                    "missing CMake config %s: build %s first" % (config, guess)
                )
            else:
                need_chain.setdefault(pkg, []).append(
                    "missing CMake config %s: build its provider first" % config
                )
        if "nothing provides" in combined:
            providers = sorted(
                {
                    match.group(1).strip()
                    for match in re.finditer(
                        r"nothing provides ([^\n]+?) needed by", combined
                    )
                }
            )
            if providers:
                need_chain.setdefault(pkg, []).extend(
                    "missing provider: %s" % item for item in providers[:10]
                )
        if _fastfix_patch_failure(combined):
            need_manual.setdefault(pkg, []).append("%prep patch failure needs review")
        if _fastfix_source_hash_failure(combined):
            need_manual.setdefault(pkg, []).append("source checksum failure needs review")
        if actions and not dry_run:
            spec_path.write_text(spec_text, encoding="utf-8")
        if actions:
            fixed += 1
            click.echo("%s  %s%s" % (pkg, "would fix: " if dry_run else "fixed: ", "; ".join(actions)))
        elif pkg not in need_chain and pkg not in need_manual:
            click.echo("%s  note: no actionable failure signature in logs" % pkg)
    for pkg in sorted(need_chain):
        for note in need_chain[pkg]:
            click.echo("%s  chain: %s" % (pkg, note))
    for pkg in sorted(need_manual):
        for note in need_manual[pkg]:
            click.echo("%s  manual: %s" % (pkg, note), err=True)
    click.echo(
        "packages=%d %s=%d need-chain=%d need-manual=%d"
        % (
            len(by_package),
            "would-fix" if dry_run else "fixed",
            fixed,
            len(need_chain),
            len(need_manual),
        )
    )


@cli.command("build")
@click.argument("packages", nargs=-1, required=True)
@click.option("--release", "release_ref", required=True, help="Release id or releasename (e.g. oreon-11-rp1)")
@click.option(
    "--arch",
    default="x86_64,aarch64",
    help="Comma-separated arches (default x86_64,aarch64)",
)
@click.option(
    "--log",
    "log_dir",
    type=click.Path(),
    default=None,
    help="Directory for per-job logs (build-<job_id>.log); polls until each job finishes then downloads",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Poll until each job reaches a terminal state. With --log, waiting always happens so logs can be saved.",
)
@click.option(
    "--skip-complete/--no-skip-complete",
    "skip_complete",
    default=True,
    help="Skip arches that already have a successful build (API default)",
)
@click.option(
    "--distgit-url",
    envvar="ORBSCLI_DISTGIT_URL",
    default="https://github.com/oreonhq/rpm-specfiles.git",
    help="Git URL for distgit sources",
)
@click.option(
    "--distgit-branch",
    envvar="ORBSCLI_DISTGIT_BRANCH",
    default="main",
    help="Branch fragment in distgit URL",
)
@click.pass_context
def cmd_build(
    ctx: click.Context,
    packages: tuple[str, ...],
    release_ref: str,
    arch: str,
    log_dir: str | None,
    wait: bool,
    skip_complete: bool,
    distgit_url: str,
    distgit_branch: str,
) -> None:
    token = require_auth(get_token())
    require_maintainer(token)
    api_base = get_api_url()
    rid = resolve_release_id(api_base, token, release_ref)
    rel = http_json("GET", api_base, f"/api/releases/{rid}", token=token)
    releasename = rel.get("releasename") or ""
    if not releasename:
        click.echo("Release has no releasename", err=True)
        sys.exit(1)
    arches = [a.strip() for a in arch.split(",") if a.strip()]
    names = parse_package_names(packages)
    if not names:
        click.echo("No package names given.", err=True)
        sys.exit(2)
    log_path = Path(log_dir) if log_dir else None
    if log_path:
        log_path.mkdir(parents=True, exist_ok=True)

    jobs_out: list[dict[str, Any]] = []
    if len(names) > 1:
        batch_body: dict[str, Any] = {
            "release_id": rid,
            "packages": [{"name": n} for n in names],
            "architectures": arches,
            "skip_complete_arches": skip_complete,
            "distgit_url": distgit_url,
            "distgit_branch": distgit_branch,
        }
        out = http_json(
            "POST",
            api_base,
            "/api/builds/trigger-batch",
            token=token,
            json_body=batch_body,
            timeout=60.0,
        )
        msg = (out or {}).get("message") if isinstance(out, dict) else None
        n = (out or {}).get("package_count") if isinstance(out, dict) else len(names)
        click.echo(msg or ("distgit batch accepted for %s packages" % n))
        return

    for pkg in names:
        body = {
                "new_package": distgit_new_package_payload(
                    pkg, releasename, distgit_url=distgit_url, distgit_branch=distgit_branch
                ),
                "release_id": rid,
                "architectures": arches,
                "skip_complete_arches": skip_complete,
            }
        created = http_json(
            "POST",
            api_base,
            "/api/builds/trigger?background=false",
            token=token,
            json_body=body,
            timeout=300.0,
        )
        if not isinstance(created, list):
            click.echo("Unexpected trigger response", err=True)
            sys.exit(1)
        for job in created:
            jid = int(job["id"])
            click.echo("queued build job %s package=%s arch=%s" % (jid, pkg, job.get("architecture") or ""))
            jobs_out.append(job)

    need_poll = wait or log_path is not None
    if need_poll and jobs_out:
        for job in jobs_out:
            _wait_job_and_maybe_log(api_base, token, int(job["id"]), log_path)

@cli.command("chain-build")
@click.argument("chain")
@click.option("--release", "release_ref", required=True, help="Release id or releasename")
@click.option("--side-tag", "side_tag_ref", required=True, help="Side tag id to publish into")
@click.option("--arch", default="x86_64,aarch64", help="Comma-separated architectures")
@click.option(
    "--mock-environment-id",
    type=int,
    default=None,
    help="Manual Mock environment for every package in the chain",
)
@click.option(
    "--distgit-url",
    envvar="ORBSCLI_DISTGIT_URL",
    default="https://github.com/oreonhq/rpm-specfiles.git",
)
@click.option("--distgit-branch", envvar="ORBSCLI_DISTGIT_BRANCH", default="main")
def cmd_chain_build(
    chain: str,
    release_ref: str,
    side_tag_ref: str,
    arch: str,
    mock_environment_id: int | None,
    distgit_url: str,
    distgit_branch: str,
) -> None:
    """Run an explicit ordered build chain: 'a b : c : d e'."""
    token = require_auth(get_token())
    require_maintainer(token)
    api_base = get_api_url()
    rid = resolve_release_id(api_base, token, release_ref)
    body: dict[str, Any] = {
        "chain": chain,
        "release_id": rid,
        "side_tag_id": int(side_tag_ref),
        "architectures": [a.strip() for a in arch.split(",") if a.strip()],
        "distgit_url": distgit_url,
        "distgit_branch": distgit_branch,
    }
    if mock_environment_id is not None:
        body["preferred_mock_environment_id"] = mock_environment_id
    data = http_json(
        "POST",
        api_base,
        "/api/builds/chain-trigger",
        token=token,
        json_body=body,
        timeout=300.0,
    )
    click.echo(
        "chain_id=%s status=%s groups=%s queued_jobs=%s"
        % (data.get("id"), data.get("status"), data.get("total_layers"), data.get("queued_jobs"))
    )


def _wait_job_and_maybe_log(api_base: str, token: str, job_id: int, log_dir: Path | None) -> None:
    terminal = {"success", "failed", "cancelled", "skipped"}
    attempt_id: int | None = None
    while True:
        j = http_json("GET", api_base, f"/api/builds/jobs/{job_id}", token=token)
        st = (j.get("status") or "").lower()
        att = http_json("GET", api_base, f"/api/builds/jobs/{job_id}/attempts", token=token)
        latest = att[0] if att else None
        if latest:
            attempt_id = int(latest["id"])
        if st in terminal:
            click.echo("job %s finished: %s" % (job_id, st))
            break
        time.sleep(5)
    if log_dir and attempt_id:
        _fetch_attempt_log(api_base, attempt_id, log_dir / f"build-{job_id}.log")

def _fetch_attempt_log(api_base: str, attempt_id: int, dest: Path) -> bool:
    r = http_request_raw("GET", api_base, f"/api/logs/attempts/{attempt_id}", token=None, timeout=300.0)
    if r.status_code != 200:
        click.echo("log not available for attempt %s (HTTP %s)" % (attempt_id, r.status_code), err=True)
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    click.echo("wrote %s" % dest)
    return True


def _safe_log_name(value: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._+-]+", "_", (value or "").strip())
    return s.strip("._") or "unknown"


def _list_failed_chain_jobs(api_base: str, token: str | None, chain_id: int) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[int] = set()
    while True:
        data = http_json("GET", api_base, f"/api/builds/chain/{chain_id}/status",
                         token=token, params={"offset": len(jobs)}, timeout=30.0)
        page = data.get("jobs") or []
        total = int((data.get("counts") or {}).get("failed") or 0)
        for job in page:
            jid = int(job["job_id"])
            if jid in seen:
                raise click.ClickException("Server does not support chain log pagination; update the API.")
            seen.add(jid)
            jobs.append(dict(job, id=jid))
        click.echo(f"Found {len(jobs)}/{total} failed chain jobs", err=True)
        if len(jobs) >= total:
            return jobs
        if not page:
            raise click.ClickException("Server returned an incomplete chain job list.")

@cli.group("builds")
def grp_builds() -> None:
    pass

_BUILD_JOB_STATUSES = (
    "pending",
    "queued",
    "running",
    "success",
    "failed",
    "cancelled",
    "skipped",
)

@grp_builds.command("list")
@click.option("--release-id", type=int, default=None)
@click.option("--package-id", type=int, default=None)
@click.option(
    "--status",
    default=None,
    help="Filter by job status: %s (case-insensitive)"
    % ", ".join(_BUILD_JOB_STATUSES),
)
@click.option(
    "--search",
    default=None,
    help="Substring match on job id, package name, release name, arch, or status (API search)",
)
@click.option("--limit", type=int, default=50)
@click.option("--offset", type=int, default=0)
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON (items + total)")
def builds_list(
    release_id: int | None,
    package_id: int | None,
    status: str | None,
    search: str | None,
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    token = get_token()
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if release_id is not None:
        params["release_id"] = release_id
    if package_id is not None:
        params["package_id"] = package_id
    if status:
        st = status.strip().lower()
        if st not in _BUILD_JOB_STATUSES:
            click.echo(
                "Unknown status %r. Use one of: %s"
                % (status, ", ".join(_BUILD_JOB_STATUSES)),
                err=True,
            )
            sys.exit(2)
        params["status"] = st
    if search:
        params["search"] = search.strip()
    out = http_json("GET", get_api_url(), "/api/builds", token=token, params=params)
    if as_json:
        click.echo(json.dumps(out, indent=2, default=str))
        return
    total = out.get("total", "?")
    click.echo("# total=%s (showing up to %s from offset %s)" % (total, limit, offset))
    click.echo("id\tpkg_id\trel_id\tarch\tstatus\tdisplay\tsigning\tcreated")
    for j in out.get("items", []):
        ca = j.get("created_at") or ""
        if isinstance(ca, str) and len(ca) > 19:
            ca = ca[:19]
        click.echo(
            "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s"
            % (
                j.get("id"),
                j.get("package_id"),
                j.get("release_id"),
                j.get("architecture") or "-",
                j.get("status"),
                (j.get("display_status") or "-")[:24],
                (j.get("signing_status") or "-")[:12],
                ca,
            )
        )

@grp_builds.command("show")
@click.argument("job_id", type=int)
def builds_show(job_id: int) -> None:
    token = get_token()
    j = http_json("GET", get_api_url(), f"/api/builds/jobs/{job_id}", token=token)
    click.echo(json.dumps(j, indent=2))

@grp_builds.command("retry")
@click.argument("job_id", type=int)
def builds_retry(job_id: int) -> None:
    token = require_auth(get_token())
    require_maintainer(token)
    http_json("POST", get_api_url(), f"/api/builds/jobs/{job_id}/retry", token=token)
    click.echo("retry ok job %s" % job_id)

@grp_builds.command("cancel")
@click.argument("job_id", type=int)
def builds_cancel(job_id: int) -> None:
    token = require_auth(get_token())
    require_maintainer(token)
    http_json("POST", get_api_url(), f"/api/builds/jobs/{job_id}/cancel", token=token)
    click.echo("cancel ok job %s" % job_id)

@grp_builds.command("failures")
@click.option("--release-id", type=int, default=None)
def builds_failures(release_id: int | None) -> None:
    token = get_token()
    params = {}
    if release_id is not None:
        params["release_id"] = release_id
    out = http_json("GET", get_api_url(), "/api/builds/current-failures", token=token, params=params or None)
    for row in out:
        click.echo(
            "%s\t%s\t%s\t%s"
            % (
                row.get("id"),
                row.get("package_name"),
                row.get("release_name"),
                row.get("architecture") or "-",
            )
        )

@grp_builds.command("fix-current")
@click.option("--release-id", type=int, default=None, help="Same filter as builds failures (optional).")
@click.option(
    "--mode",
    type=click.Choice(["retry", "rebuild"]),
    default="retry",
    help="retry POSTs each failing job id (default). rebuild queues one trigger per package+release with skip_complete_arches on (no duplicate work for already-green arches).",
)
@click.option("--dry-run", is_flag=True, help="Print actions only, no POSTs.")
@click.option(
    "--arch",
    default="",
    help="Rebuild mode only. If set, comma-separated arches passed to /api/builds/trigger. If omitted, reuse the failing arch(es) from current-failures rows per package.",
)
@click.option(
    "--distgit-url",
    envvar="ORBSCLI_DISTGIT_URL",
    default="https://github.com/oreonhq/rpm-specfiles.git",
)
@click.option(
    "--distgit-branch",
    envvar="ORBSCLI_DISTGIT_BRANCH",
    default="main",
)
def builds_fix_current(
    release_id: int | None,
    mode: str,
    dry_run: bool,
    arch: str,
    distgit_url: str,
    distgit_branch: str,
) -> None:
    token_read = get_token()
    params: dict[str, Any] = {}
    if release_id is not None:
        params["release_id"] = release_id
    out = http_json(
        "GET",
        get_api_url(),
        "/api/builds/current-failures",
        token=token_read,
        params=params or None,
    )
    if not isinstance(out, list):
        click.echo("Unexpected current-failures response", err=True)
        sys.exit(1)
    if not out:
        click.echo("No current failures.")
        return

    api_base = get_api_url()

    if mode == "retry":
        if dry_run:
            seen: set[int] = set()
            for row in out:
                jid = row.get("id")
                if jid is None:
                    continue
                jid = int(jid)
                if jid in seen:
                    continue
                seen.add(jid)
                click.echo(
                    "dry-run retry job %s package=%s arch=%s"
                    % (jid, row.get("package_name") or "?", row.get("architecture") or "-")
                )
            return
        token = require_auth(get_token())
        require_maintainer(token)
        seen_retry: set[int] = set()
        for row in out:
            jid = row.get("id")
            if jid is None:
                continue
            jid = int(jid)
            if jid in seen_retry:
                continue
            seen_retry.add(jid)
            http_json("POST", api_base, f"/api/builds/jobs/{jid}/retry", token=token)
            click.echo(
                "retry ok job %s package=%s arch=%s"
                % (jid, row.get("package_name") or "?", row.get("architecture") or "-")
            )
        return

    explicit_arches = [a.strip() for a in arch.split(",") if a.strip()]
    keys: dict[tuple[int, str], dict[str, Any]] = {}
    arches_for_key: dict[tuple[int, str], set[str]] = {}
    for row in out:
        rid = row.get("release_id")
        pkg = row.get("package_name")
        row_arch = str(row.get("architecture") or "").strip()
        if rid is None and row.get("release_name"):
            try:
                rid = resolve_release_id(api_base, token_read, str(row["release_name"]))
            except SystemExit:
                rid = None
        if rid is None or not pkg:
            click.echo("skip row missing release_id or package_name: %s" % (row,), err=True)
            continue
        rid = int(rid)
        key = (rid, str(pkg))
        keys[key] = row
        if row_arch:
            arches_for_key.setdefault(key, set()).add(row_arch)

    if dry_run:
        for rid, pkg in sorted(keys.keys(), key=lambda t: (t[0], t[1].lower())):
            key = (rid, pkg)
            arcs = explicit_arches or sorted(arches_for_key.get(key, set()))
            if not arcs:
                arcs = ["x86_64", "aarch64"]
            click.echo(
                "dry-run rebuild release_id=%s package=%s skip_complete_arches=yes arches=%s"
                % (rid, pkg, ",".join(arcs))
            )
        return

    token = require_auth(get_token())
    require_maintainer(token)
    release_names: dict[int, str] = {}
    for rid, pkg in sorted(keys.keys(), key=lambda t: (t[0], t[1].lower())):
        key = (rid, pkg)
        arcs = explicit_arches or sorted(arches_for_key.get(key, set()))
        if not arcs:
            click.echo("skip package %s release %s, no inferred failing arches (set --arch to override)" % (pkg, rid), err=True)
            continue
        if rid not in release_names:
            rel = http_json("GET", api_base, f"/api/releases/{rid}", token=token)
            rn = str(rel.get("releasename") or "").strip()
            if not rn:
                click.echo("Release %s has no releasename, skip package %s" % (rid, pkg), err=True)
                continue
            release_names[rid] = rn
        releasename = release_names[rid]
        body = {
            "new_package": distgit_new_package_payload(
                pkg, releasename, distgit_url=distgit_url, distgit_branch=distgit_branch
            ),
            "release_id": rid,
            "architectures": arcs,
            "skip_complete_arches": True,
        }
        created = http_json(
            "POST",
            api_base,
            "/api/builds/trigger?background=false",
            token=token,
            json_body=body,
            timeout=300.0,
        )
        if not isinstance(created, list):
            click.echo("Unexpected trigger response for %s" % pkg, err=True)
            sys.exit(1)
        for job in created:
            click.echo(
                "queued build job %s package=%s arch=%s"
                % (int(job["id"]), pkg, job.get("architecture") or "")
            )

@cli.group("logs")
def grp_logs() -> None:
    pass

@grp_logs.command("job")
@click.argument("job_id", type=int)
@click.option("--tail", type=int, default=120, help="Last N log lines (default 120)")
@click.option("--attempt", type=int, default=None, help="Use this attempt id instead of latest for the job")
def logs_job(job_id: int, tail: int, attempt: int | None) -> None:
    token = get_token()
    base = get_api_url()
    if attempt is not None:
        aid = attempt
        click.echo("job %s attempt %s (explicit)" % (job_id, aid), err=True)
    else:
        att = http_json("GET", base, f"/api/builds/jobs/{job_id}/attempts", token=token)
        if not att:
            click.echo("No attempts for job %s" % job_id, err=True)
            sys.exit(1)
        aid = int(att[0]["id"])
        click.echo("job %s latest_attempt %s" % (job_id, aid), err=True)
    ctx = click.get_current_context()
    ctx.invoke(logs_view, attempt_id=aid, out=None, tail=tail)

@grp_logs.command("view")
@click.argument("attempt_id", type=int)
@click.option("--out", type=click.Path(), default=None, help="Write full log to file instead of stdout")
@click.option("--tail", type=int, default=None, help="If set, show last N lines as JSON (no file write)")
def logs_view(attempt_id: int, out: str | None, tail: int | None) -> None:
    base = get_api_url()
    if tail is not None:
        data = http_json("GET", base, f"/api/logs/attempts/{attempt_id}/tail", token=None, params={"lines": tail})
        for line in data.get("lines", []):
            click.echo(line)
        return
    r = http_request_raw("GET", base, f"/api/logs/attempts/{attempt_id}", token=None)
    if r.status_code != 200:
        click.echo("log not available (HTTP %s)" % r.status_code, err=True)
        sys.exit(1)
    if out:
        Path(out).write_bytes(r.content)
        click.echo("wrote %s" % out)
    else:
        click.echo(r.text, nl=False)

@grp_logs.command("chain-fail")
@click.argument("chain_id", type=int)
@click.option("--out", "out_dir", type=click.Path(), default=None,
              help="Directory to write logs into (default ./chain-<id>-failed-logs)")
def logs_chain_fail(chain_id: int, out_dir: str | None) -> None:
    _download_failed_plan_logs(chain_id, out_dir)


def _download_failed_plan_logs(chain_id: int, out_dir: str | None) -> None:
    token = get_token()
    api_base = get_api_url()
    dest = Path(out_dir) if out_dir else Path("chain-%s-failed-logs" % chain_id)
    dest.mkdir(parents=True, exist_ok=True)
    click.echo("Fetching failed jobs for chain %s..." % chain_id, err=True)
    try:
        jobs = _list_failed_chain_jobs(api_base, token, chain_id)
    except httpx.RequestError as exc:
        raise click.ClickException(f"Could not fetch failed jobs: {exc}") from exc
    if not jobs:
        click.echo("no failed jobs in chain %s" % chain_id)
        return
    wrote = 0
    missing = 0
    for index, job in enumerate(jobs, 1):
        jid = int(job["id"])
        click.echo(f"[{index}/{len(jobs)}] Fetching attempts and log for job {jid}...", err=True)
        pkg = _safe_log_name(str(job.get("package_name") or "pkg-%s" % (job.get("package_id") or jid)))
        arch = _safe_log_name(str(job.get("architecture") or "noarch"))
        attempts = http_json("GET", api_base, f"/api/builds/jobs/{jid}/attempts", token=token)
        if not isinstance(attempts, list) or not attempts:
            click.echo("job %s %s %s has no attempts" % (jid, pkg, arch), err=True)
            missing += 1
            continue
        att = next((a for a in attempts if isinstance(a, dict)), None)
        if att is None:
            click.echo("job %s %s %s has no attempts" % (jid, pkg, arch), err=True)
            missing += 1
            continue
        aid = int(att["id"])
        name = "%s-%s-job%s.log" % (pkg, arch, jid)
        if _fetch_attempt_log(api_base, aid, dest / name):
            wrote += 1
        else:
            missing += 1
    click.echo("chain %s failed jobs=%s logs written=%s missing=%s dir=%s" % (
        chain_id,
        len(jobs),
        wrote,
        missing,
        dest,
    ))

@cli.command("promote")
@click.argument("from_channel")
@click.argument("to_channel")
@click.option("--release-id", type=int, required=True)
@click.pass_context
def cmd_promote(
    ctx: click.Context,
    from_channel: str,
    to_channel: str,
    release_id: int,
) -> None:
    token = require_auth(get_token())
    require_maintainer(token)
    body: dict[str, Any] = {
        "release_id": release_id,
        "from_branch": from_channel,
        "to_branch": to_channel,
    }
    out = http_json("POST", get_api_url(), "/api/promotions/branch", token=token, json_body=body)
    click.echo(json.dumps(out, indent=2))

@cli.group("promotions")
def grp_promotions() -> None:
    pass

@grp_promotions.command("branch")
@click.option("--release-id", type=int, required=True)
@click.option("--from-branch", required=True)
@click.option("--to-branch", required=True)
@click.pass_context
def promotions_branch(
    ctx: click.Context, release_id: int, from_branch: str, to_branch: str
) -> None:
    token = require_auth(get_token())
    require_maintainer(token)
    out = http_json(
        "POST",
        get_api_url(),
        "/api/promotions/branch",
        token=token,
        json_body={
            "release_id": release_id,
            "from_branch": from_branch,
            "to_branch": to_branch,
        },
    )
    click.echo(json.dumps(out, indent=2))

@grp_promotions.command("list")
@click.option("--release-id", type=int, default=None)
@click.option("--status", default=None)
@click.option("--limit", type=int, default=50)
@click.pass_context
def promotions_list(ctx: click.Context, release_id: int | None, status: str | None, limit: int) -> None:
    click.echo(
        "Promotion DB queue is gone. Use: orbs promote FROM TO --release-id ID "
        "(whole-tree R2 copy, empties source)."
    )

@cli.group("mockenv")
def grp_mockenv() -> None:
    pass

@grp_mockenv.command("list")
@click.option("--release-id", type=int, default=None)
@click.pass_context
def mockenv_list(ctx: click.Context, release_id: int | None) -> None:
    token = get_token()
    params: dict[str, Any] = {"limit": 200, "offset": 0}
    if release_id is not None:
        params["release_id"] = release_id
    out = http_json("GET", get_api_url(), "/api/mock-environments", token=token, params=params)
    for e in out.get("items", []):
        click.echo("%s\t%s\trelease=%s\t%s" % (e.get("id"), e.get("name"), e.get("release_id"), e.get("config_name")))

@grp_mockenv.command("show")
@click.argument("env_id", type=int)
@click.pass_context
def mockenv_show(ctx: click.Context, env_id: int) -> None:
    token = get_token()
    out = http_json("GET", get_api_url(), f"/api/mock-environments/{env_id}", token=token)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_mockenv.command("create")
@click.option("--release-id", type=int, required=True)
@click.option("--name", required=True)
@click.option("--config-name", required=True)
@click.option("--config-content", default="")
@click.option("--priority", type=int, default=0)
@click.option("--available/--unavailable", default=True)
@click.pass_context
def mockenv_create(
    ctx: click.Context,
    release_id: int,
    name: str,
    config_name: str,
    config_content: str,
    priority: int,
    available: bool,
) -> None:
    token = require_auth(get_token())
    require_admin(token)
    body = {
        "release_id": release_id,
        "name": name,
        "config_name": config_name,
        "config_content": config_content or None,
        "additional_repo_urls": [],
        "is_available": available,
        "priority": priority,
    }
    out = http_json("POST", get_api_url(), "/api/mock-environments", token=token, json_body=body)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_mockenv.command("update")
@click.argument("env_id", type=int)
@click.option("--name", default=None)
@click.option("--config-name", default=None)
@click.option("--config-content", default=None)
@click.option("--priority", type=int, default=None)
@click.option("--available/--unavailable", default=None)
@click.pass_context
def mockenv_update(
    ctx: click.Context,
    env_id: int,
    name: str | None,
    config_name: str | None,
    config_content: str | None,
    priority: int | None,
    available: bool | None,
) -> None:
    token = require_auth(get_token())
    require_admin(token)
    patch: dict[str, Any] = {}
    if name is not None:
        patch["name"] = name
    if config_name is not None:
        patch["config_name"] = config_name
    if config_content is not None:
        patch["config_content"] = config_content
    if priority is not None:
        patch["priority"] = priority
    if available is not None:
        patch["is_available"] = available
    out = http_json("PATCH", get_api_url(), f"/api/mock-environments/{env_id}", token=token, json_body=patch)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_mockenv.command("delete")
@click.argument("env_id", type=int)
@click.pass_context
def mockenv_delete(ctx: click.Context, env_id: int) -> None:
    token = require_auth(get_token())
    require_admin(token)
    http_json("DELETE", get_api_url(), f"/api/mock-environments/{env_id}", token=token)
    click.echo("deleted mock env %s" % env_id)

@cli.group("worker")
def grp_worker() -> None:
    pass

@grp_worker.command("list")
@click.pass_context
def worker_list(ctx: click.Context) -> None:
    out = http_json("GET", get_api_url(), "/api/workers", token=get_token())
    for w in out.get("items", []):
        click.echo("%s\t%s\t%s\t%s" % (w.get("id"), w.get("name"), w.get("state"), w.get("architecture") or ""))

@grp_worker.command("enroll")
@click.option("--name", required=True)
@click.option("--architecture", default=None)
@click.option(
    "--secret",
    "enrollment_secret",
    envvar=["OREON_WORKER_ENROLLMENT_SECRET", "ORBSCLI_WORKER_ENROLLMENT_SECRET"],
    required=True,
)
@click.pass_context
def worker_enroll(ctx: click.Context, name: str, architecture: str | None, enrollment_secret: str) -> None:
    token = require_auth(get_token())
    require_admin(token)
    body = {"name": name, "enrollment_token": enrollment_secret, "architecture": architecture}
    out = http_json("POST", get_api_url(), "/api/workers/enroll", token=token, json_body=body)
    click.echo(json.dumps(out, indent=2))

@grp_worker.command("delete")
@click.argument("worker_id", type=int)
@click.pass_context
def worker_delete(ctx: click.Context, worker_id: int) -> None:
    token = require_auth(get_token())
    require_admin(token)
    http_json("DELETE", get_api_url(), f"/api/workers/{worker_id}", token=token)
    click.echo("deleted worker %s" % worker_id)

@cli.group("release")
def grp_release() -> None:
    pass

@grp_release.command("list")
@click.pass_context
def release_list(ctx: click.Context) -> None:
    out = http_json("GET", get_api_url(), "/api/releases", token=get_token(), params={"limit": 500, "offset": 0})
    for r in out.get("items", []):
        click.echo("%s\t%s\tactive=%s" % (r.get("id"), r.get("releasename"), r.get("is_active")))

@grp_release.command("show")
@click.argument("release_id", type=int)
@click.pass_context
def release_show(ctx: click.Context, release_id: int) -> None:
    out = http_json("GET", get_api_url(), f"/api/releases/{release_id}", token=get_token())
    click.echo(json.dumps(out, indent=2, default=str))

@grp_release.command("create")
@click.option("--releasename", required=True)
@click.option("--description", default="")
@click.option("--architectures", default="x86_64,aarch64")
@click.option("--default-channel", default="dev")
@click.option("--default-repo-branch", default="dev")
@click.pass_context
def release_create(
    ctx: click.Context,
    releasename: str,
    description: str,
    architectures: str,
    default_channel: str,
    default_repo_branch: str,
) -> None:
    token = require_auth(get_token())
    require_admin(token)
    body = {
        "releasename": releasename,
        "description": description or None,
        "architectures": architectures,
        "default_channel": default_channel,
        "default_repo_branch": default_repo_branch,
        "base_repos": [],
    }
    out = http_json("POST", get_api_url(), "/api/releases", token=token, json_body=body)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_release.command("update")
@click.argument("release_id", type=int)
@click.option("--description", default=None)
@click.option("--architectures", default=None, help="Comma-separated")
@click.option("--default-channel", default=None)
@click.option("--default-repo-branch", default=None)
@click.option("--deactivate", is_flag=True, help="Set is_active=false (end of support)")
@click.option("--activate", is_flag=True, help="Set is_active=true")
@click.pass_context
def release_update(
    ctx: click.Context,
    release_id: int,
    description: str | None,
    architectures: str | None,
    default_channel: str | None,
    default_repo_branch: str | None,
    deactivate: bool,
    activate: bool,
) -> None:
    token = require_auth(get_token())
    require_admin(token)
    patch: dict[str, Any] = {}
    if description is not None:
        patch["description"] = description
    if architectures is not None:
        arch_list = [a.strip() for a in architectures.split(",") if a.strip()]
        patch["architectures"] = ",".join(arch_list)
    if default_channel is not None:
        patch["default_channel"] = default_channel
    if default_repo_branch is not None:
        patch["default_repo_branch"] = default_repo_branch
    if deactivate:
        patch["is_active"] = False
    if activate:
        patch["is_active"] = True
    if not patch:
        click.echo("No fields to update.", err=True)
        sys.exit(2)
    out = http_json("PATCH", get_api_url(), f"/api/releases/{release_id}", token=token, json_body=patch)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_release.command("delete")
@click.argument("release_id", type=int)
@click.pass_context
def release_delete(ctx: click.Context, release_id: int) -> None:
    token = require_auth(get_token())
    require_admin(token)
    http_json("DELETE", get_api_url(), f"/api/releases/{release_id}", token=token)
    click.echo("deleted release %s" % release_id)

@cli.group("package")
def grp_package() -> None:
    pass

@grp_package.command("list")
@click.option("--search", default=None)
@click.option("--limit", type=int, default=100)
@click.pass_context
def package_list(ctx: click.Context, search: str | None, limit: int) -> None:
    token = get_token()
    params: dict[str, Any] = {"limit": limit, "offset": 0}
    if search:
        params["search"] = search
    out = http_json("GET", get_api_url(), "/api/packages", token=token, params=params)
    for p in out.get("items", []):
        click.echo("%s\t%s" % (p.get("id"), p.get("name")))

@grp_package.command("show")
@click.argument("package_id", type=int)
@click.pass_context
def package_show(ctx: click.Context, package_id: int) -> None:
    out = http_json("GET", get_api_url(), f"/api/packages/{package_id}", token=get_token())
    click.echo(json.dumps(out, indent=2, default=str))

@grp_package.command("create")
@click.option("--name", required=True)
@click.option("--description", default=None)
@click.option("--github-repository-id", type=int, default=None)
@click.option("--github-html-url", default=None)
@click.pass_context
def package_create(
    ctx: click.Context,
    name: str,
    description: str | None,
    github_repository_id: int | None,
    github_html_url: str | None,
) -> None:
    token = require_auth(get_token())
    require_maintainer(token)
    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description
    if github_repository_id is not None:
        body["github_repository_id"] = github_repository_id
    if github_html_url:
        body["github_html_url"] = github_html_url
    out = http_json("POST", get_api_url(), "/api/packages", token=token, json_body=body)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_package.command("update")
@click.argument("package_id", type=int)
@click.option("--description", default=None)
@click.option("--github-repository-id", type=int, default=None)
@click.option("--github-html-url", default=None)
@click.option("--preferred-mock-environment-id", type=int, default=None)
@click.pass_context
def package_update(
    ctx: click.Context,
    package_id: int,
    description: str | None,
    github_repository_id: int | None,
    github_html_url: str | None,
    preferred_mock_environment_id: int | None,
) -> None:
    token = require_auth(get_token())
    require_maintainer(token)
    patch: dict[str, Any] = {}
    if description is not None:
        patch["description"] = description
    if github_repository_id is not None:
        patch["github_repository_id"] = github_repository_id
    if github_html_url is not None:
        patch["github_html_url"] = github_html_url
    if preferred_mock_environment_id is not None:
        patch["preferred_mock_environment_id"] = preferred_mock_environment_id
    out = http_json("PATCH", get_api_url(), f"/api/packages/{package_id}", token=token, json_body=patch)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_package.command("delete")
@click.argument("package_id", type=int)
@click.pass_context
def package_delete(ctx: click.Context, package_id: int) -> None:
    token = require_auth(get_token())
    require_admin(token)
    http_json("DELETE", get_api_url(), f"/api/packages/{package_id}", token=token)
    click.echo("deleted package %s" % package_id)

@cli.group("schedule")
def grp_schedule() -> None:
    pass

@grp_schedule.command("list")
@click.pass_context
def schedule_list(ctx: click.Context) -> None:
    token = require_auth(get_token())
    require_admin(token)
    out = http_json("GET", get_api_url(), "/api/schedules", token=token)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_schedule.command("show")
@click.argument("schedule_id", type=int)
@click.pass_context
def schedule_show(ctx: click.Context, schedule_id: int) -> None:
    token = require_auth(get_token())
    require_admin(token)
    out = http_json("GET", get_api_url(), f"/api/schedules/{schedule_id}", token=token)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_schedule.command("create")
@click.option("--release-id", type=int, required=True)
@click.option("--name", required=True)
@click.option("--kind", required=True)
@click.option("--cron", "cron_expression", default=None)
@click.option("--config", default=None)
@click.option("--enabled/--disabled", "is_enabled", default=True)
@click.pass_context
def schedule_create(
    ctx: click.Context,
    release_id: int,
    name: str,
    kind: str,
    cron_expression: str | None,
    config: str | None,
    is_enabled: bool,
) -> None:
    token = require_auth(get_token())
    require_admin(token)
    body = {
        "release_id": release_id,
        "name": name,
        "kind": kind,
        "cron_expression": cron_expression,
        "config": config,
        "is_enabled": is_enabled,
    }
    out = http_json("POST", get_api_url(), "/api/schedules", token=token, json_body=body)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_schedule.command("update")
@click.argument("schedule_id", type=int)
@click.option("--cron", "cron_expression", default=None)
@click.option("--config", default=None)
@click.option("--enabled/--disabled", "is_enabled", default=None)
@click.pass_context
def schedule_update(
    ctx: click.Context,
    schedule_id: int,
    cron_expression: str | None,
    config: str | None,
    is_enabled: bool | None,
) -> None:
    token = require_auth(get_token())
    require_admin(token)
    patch: dict[str, Any] = {}
    if cron_expression is not None:
        patch["cron_expression"] = cron_expression
    if config is not None:
        patch["config"] = config
    if is_enabled is not None:
        patch["is_enabled"] = is_enabled
    out = http_json("PATCH", get_api_url(), f"/api/schedules/{schedule_id}", token=token, json_body=patch)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_schedule.command("delete")
@click.argument("schedule_id", type=int)
@click.pass_context
def schedule_delete(ctx: click.Context, schedule_id: int) -> None:
    token = require_auth(get_token())
    require_admin(token)
    http_json("DELETE", get_api_url(), f"/api/schedules/{schedule_id}", token=token)
    click.echo("deleted schedule %s" % schedule_id)

@cli.group("repo")
def grp_repo() -> None:
    pass

@grp_repo.command("status")
@click.option("--release-id", type=int, default=None)
@click.pass_context
def repo_status(ctx: click.Context, release_id: int | None) -> None:
    token = get_token()
    params = {}
    if release_id is not None:
        params["release_id"] = release_id
    out = http_json("GET", get_api_url(), "/api/repos/status", token=token, params=params or None)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_repo.command("publish")
@click.option("--release-id", type=int, required=True)
@click.option("--channel", required=True)
@click.option("--architecture", required=True)
@click.pass_context
def repo_publish(ctx: click.Context, release_id: int, channel: str, architecture: str) -> None:
    token = require_auth(get_token())
    require_maintainer(token)
    http_json(
        "POST",
        get_api_url(),
        "/api/repos/publish",
        token=token,
        params={"release_id": release_id, "channel": channel, "architecture": architecture},
    )
    click.echo("publish requested")

@grp_repo.command("regenerate-metadata")
@click.pass_context
def repo_regenerate_metadata(ctx: click.Context) -> None:
    token = require_auth(get_token())
    require_admin(token)
    out = http_json("POST", get_api_url(), "/api/repos/regenerate-metadata", token=token)
    click.echo(json.dumps(out, indent=2, default=str))

@cli.group("audit")
def grp_audit() -> None:
    pass

@grp_audit.command("list")
@click.option("--limit", type=int, default=100)
@click.pass_context
def audit_list(ctx: click.Context, limit: int) -> None:
    token = require_auth(get_token())
    require_admin(token)
    out = http_json("GET", get_api_url(), "/api/audit", token=token, params={"limit": limit})
    for row in out:
        click.echo("%s\t%s\t%s\t%s" % (row.get("id"), row.get("action"), row.get("resource_type"), row.get("details")))

@cli.group("account")
def grp_account() -> None:
    pass

@grp_account.command("list")
@click.pass_context
def account_list(ctx: click.Context) -> None:
    token = require_auth(get_token())
    require_admin(token)
    out = http_json("GET", get_api_url(), "/api/accounts", token=token)
    for a in out:
        click.echo("%s\t%s\t%s\tactive=%s" % (a.get("id"), a.get("username"), a.get("role"), a.get("is_active")))

@grp_account.command("create")
@click.option("--username", required=True)
@click.option("--password", envvar="ORBSCLI_NEW_ACCOUNT_PASSWORD", required=True)
@click.option("--role", type=click.Choice(["admin", "maintainer"]), required=True)
@click.pass_context
def account_create(ctx: click.Context, username: str, password: str, role: str) -> None:
    token = require_auth(get_token())
    require_admin(token)
    out = http_json(
        "POST",
        get_api_url(),
        "/api/accounts",
        token=token,
        json_body={"username": username, "password": password, "role": role},
    )
    click.echo(json.dumps(out, indent=2, default=str))

@grp_account.command("update")
@click.argument("account_id", type=int)
@click.option("--password", envvar="ORBSCLI_ACCOUNT_PASSWORD", default=None)
@click.option("--role", type=click.Choice(["admin", "maintainer"]), default=None)
@click.option("--active/--inactive", default=None)
@click.pass_context
def account_update(
    ctx: click.Context,
    account_id: int,
    password: str | None,
    role: str | None,
    active: bool | None,
) -> None:
    token = require_auth(get_token())
    require_admin(token)
    patch: dict[str, Any] = {}
    if password is not None:
        patch["password"] = password
    if role is not None:
        patch["role"] = role
    if active is not None:
        patch["is_active"] = active
    if not patch:
        click.echo("Nothing to update", err=True)
        sys.exit(2)
    out = http_json("PATCH", get_api_url(), f"/api/accounts/{account_id}", token=token, json_body=patch)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_account.command("delete")
@click.argument("account_id", type=int)
@click.pass_context
def account_delete(ctx: click.Context, account_id: int) -> None:
    token = require_auth(get_token())
    require_admin(token)
    http_json("DELETE", get_api_url(), f"/api/accounts/{account_id}", token=token)
    click.echo("deleted account %s" % account_id)

@cli.command("search")
@click.argument("query")
@click.pass_context
def cmd_search(ctx: click.Context, query: str) -> None:
    token = get_token()
    out = http_json("GET", get_api_url(), "/api/search", token=token, params={"q": query})
    click.echo(json.dumps(out, indent=2, default=str))

@cli.group("advisory")
def grp_advisory() -> None:
    pass

@grp_advisory.command("list")
@click.option("--release-id", type=int, default=None)
@click.option("--limit", type=int, default=25)
@click.pass_context
def advisory_list(ctx: click.Context, release_id: int | None, limit: int) -> None:
    token = get_token()
    wd = get_watchdog_url()
    params: dict[str, Any] = {"limit": limit, "offset": 0}
    if release_id is not None:
        params["release_id"] = release_id
    out = http_json("GET", wd, "/api/security/advisories", token=token, params=params)
    click.echo(json.dumps(out, indent=2, default=str)[:8000])

@grp_advisory.command("scan")
@click.pass_context
def advisory_scan(ctx: click.Context) -> None:
    token = require_auth(get_token())
    require_admin(token)
    wd = get_watchdog_url()
    out = http_json("POST", wd, "/api/security/scan", token=token, json_body={})
    click.echo(json.dumps(out, indent=2, default=str))

@grp_advisory.command("create-custom")
@click.option("--title", required=True)
@click.option("--package-name", required=True)
@click.option("--package-version", required=True)
@click.option("--release-id", "release_ids", multiple=True, type=int, required=True)
@click.option("--cve-id", default=None)
@click.option("--severity", default=None)
@click.option("--summary", default="")
@click.option("--upstream-url", default=None)
@click.option("--recommended-fix", default=None)
@click.pass_context
def advisory_create_custom(
    ctx: click.Context,
    title: str,
    package_name: str,
    package_version: str,
    release_ids: tuple[int, ...],
    cve_id: str | None,
    severity: str | None,
    summary: str,
    upstream_url: str | None,
    recommended_fix: str | None,
) -> None:
    token = require_auth(get_token())
    require_maintainer(token)
    wd = get_watchdog_url()
    body = {
        "title": title,
        "cve_id": cve_id,
        "package_name": package_name,
        "package_version": package_version,
        "severity": severity,
        "upstream_url": upstream_url,
        "summary": summary,
        "recommended_fix": recommended_fix,
        "affected_release_ids": list(release_ids),
    }
    out = http_json("POST", wd, "/api/security/advisories/custom", token=token, json_body=body)
    click.echo(json.dumps(out, indent=2, default=str))

@grp_advisory.command("resolve-cve")
@click.argument("cve_match_id", type=int)
@click.option("--reason", type=click.Choice(["backport", "upstream_bump", "false_positive", "mitigated", "accepted_risk", "other"]))
@click.option("--notes", default=None)
@click.pass_context
def advisory_resolve_cve(
    ctx: click.Context,
    cve_match_id: int,
    reason: str | None,
    notes: str | None,
) -> None:
    token = require_auth(get_token())
    require_maintainer(token)
    wd = get_watchdog_url()
    body = {}
    if reason:
        body["reason"] = reason
    if notes:
        body["notes"] = notes
    out = http_json(
        "POST",
        wd,
        f"/api/security/advisories/cve/{cve_match_id}/resolve",
        token=token,
        json_body=body or None,
    )
    click.echo(json.dumps(out, indent=2, default=str))

@grp_advisory.command("resolve-custom")
@click.argument("advisory_id", type=int)
@click.pass_context
def advisory_resolve_custom(ctx: click.Context, advisory_id: int) -> None:
    token = require_auth(get_token())
    require_maintainer(token)
    wd = get_watchdog_url()
    out = http_json("POST", wd, f"/api/security/advisories/custom/{advisory_id}/resolve", token=token)
    click.echo(json.dumps(out, indent=2, default=str))

@cli.command("bulk-bump")
@click.option("--release", "release_ref", required=True, help="Oreon release name, e.g. oreon-11-rp1")
@click.option("--spec-root", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--packages", "packages_blob", default=None, help="Package names separated by spaces, commas, or newlines")
@click.option("--all", "bump_all", is_flag=True, help="Check every spec under --spec-root")
@click.option("--arch", "repo_arch", default="x86_64,aarch64", help="Repository architectures")
@click.option("--repo-base", envvar="ORBSCLI_REPO_BASE", default="https://repo-us.oreonhq.com")
@click.option("--side-tag", "side_tag_ref", default=None, help="Include this side tag when resolving dependencies")
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--verify-sources/--no-verify-sources", default=False)
@click.option("--refresh-hashes/--no-refresh-hashes", default=False)
@click.option("--verify-patches/--no-verify-patches", default=True)
@click.option("--refresh-files/--no-refresh-files", default=True)
def cmd_bulk_bump(
    release_ref: str, spec_root: Path | None, packages_blob: str | None,
    bump_all: bool, repo_arch: str, repo_base: str, side_tag_ref: str | None,
    out_dir: Path | None, verify_sources: bool, refresh_hashes: bool,
    verify_patches: bool, refresh_files: bool,
) -> None:
    """Bump upstream versions in bulk and write an ordered build chain."""
    if bump_all == (packages_blob is not None):
        raise click.UsageError("choose exactly one of --all or --packages")
    root = (spec_root.expanduser().resolve() if spec_root else
            Path(__file__).resolve().parent.parent / "fedora-rpms" / release_ref)
    if not root.is_dir():
        raise click.ClickException("spec root is not a directory: %s" % root)
    names = ([name for name, _ in _spec_dirs_with_specs(root)] if bump_all else
             list(dict.fromkeys(parse_package_blob(packages_blob or ""))))
    if not names:
        raise click.ClickException("no packages selected")
    paths: dict[str, Path] = {}
    for name in names:
        specs = find_pkg_specs(root, name)
        if len(specs) != 1:
            raise click.ClickException("%s has %d specs; expected one" % (name, len(specs)))
        paths[name] = specs[0]
    before = {name: path.read_bytes() for name, path in paths.items()}
    click.echo("Checking %d specs for upstream updates" % len(names), err=True)
    try:
        cmd_spec_bump.callback(
            packages_blob=" ".join(names), spec_root=root, bump_all=False,
            verify_sources=verify_sources, refresh_hashes=refresh_hashes,
            verify_patches=verify_patches, refresh_configs=False,
            refresh_files=refresh_files, non_interactive=True,
        )
    except SystemExit as exc:
        if exc.code:
            raise click.ClickException("some bumps failed; fix them before generating a chain") from exc
    changed = [name for name, path in paths.items() if path.read_bytes() != before[name]]
    click.echo("Changed specs: %d" % len(changed), err=True)
    if not changed:
        click.echo("All selected specs already match upstream; no chain needed.")
        return
    cmd_generate_chain.callback(
        release_ref=release_ref, pushed=False, spec_root=root,
        packages_blob=" ".join(changed), packages_file=None,
        side_tag_ref=side_tag_ref, repo_arch=repo_arch, repo_base=repo_base,
        no_repo_filter=False, stale=False, out_dir=out_dir,
    )
    destination = out_dir.expanduser().resolve() if out_dir else Path(__file__).resolve().parent.parent
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", release_ref).strip("-") or "chain"
    cycles_file = destination / ("chain-%s-fedora-44.txt" % slug)
    if cycles_file.is_file() and cycles_file.read_text(encoding="utf-8").strip():
        raise click.ClickException(
            "dependency cycles need bootstrap builds; review %s before submitting the chain" % cycles_file
        )


def main() -> None:
    cli.add_command(grp_builds)
    cli.add_command(grp_logs)
    cli.add_command(grp_promotions)
    cli.add_command(grp_mockenv)
    cli.add_command(grp_worker)
    cli.add_command(grp_release)
    cli.add_command(grp_package)
    cli.add_command(grp_schedule)
    cli.add_command(grp_repo)
    cli.add_command(grp_audit)
    cli.add_command(grp_account)
    cli.add_command(grp_advisory)
    cli()

if __name__ == "__main__":
    main()
