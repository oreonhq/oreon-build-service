from __future__ import annotations

import base64
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import click
import httpx

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "orbscli"
CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"
CONFIG_PATH = CONFIG_DIR / "config.json"

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

def _rewrite_version_defines(preamble: str, old: str, new: str) -> str:
    old_px = _dotted_prefixes(old)
    new_px = _dotted_prefixes(new)

    def _sub(m: re.Match[str]) -> str:
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
    return rx.sub(new, text)

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

def _watchdog_latest(wd: str, token: str | None, name: str, cached: dict[str, str]) -> str | None:
    hit = cached.get(name)
    if hit:
        return hit
    r = http_request_raw(
        "GET",
        wd,
        "/api/release-monitoring/latest/%s" % quote(name, safe=".-_"),
        token=token,
        timeout=60.0,
    )
    if r.status_code != 200:
        detail = ""
        try:
            detail = str(r.json().get("detail") or "")
        except Exception:
            detail = r.text[:200]
        click.echo("%s  watchdog latest failed (%s) %s" % (name, r.status_code, detail), err=True)
        return None
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    ver = str((data or {}).get("latest_version") or "").strip()
    return ver or None

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
def cmd_spec_bump(packages_blob: str | None, spec_root: Path | None) -> None:
    token = get_token()
    wd = get_watchdog_url()
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
    if spec_root is None:
        if not sys.stdin.isatty():
            click.echo("pass -d/--spec-root when stdin is not a tty", err=True)
            sys.exit(2)
        raw = click.prompt("rpm-specfiles directory")
        spec_root = Path(raw)
    root = spec_root.expanduser()
    if not root.is_dir():
        click.echo("not a directory  %s" % root, err=True)
        sys.exit(2)

    cached: dict[str, str] = {}
    rr = http_request_raw("GET", wd, "/api/release-monitoring/results", token=token, timeout=60.0)
    if rr.status_code == 200:
        try:
            payload = rr.json()
        except Exception:
            payload = {}
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            n = str(item.get("package_name") or "").strip()
            v = str(item.get("latest_version") or "").strip()
            if n and v:
                cached[n] = v

    ok = 0
    skip = 0
    fail = 0
    for pkg in names:
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
        spec_path = specs[0]
        latest = _watchdog_latest(wd, token, pkg, cached)
        if not latest:
            fail += 1
            continue
        try:
            text = spec_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            click.echo("%s  cannot read %s (%s)" % (pkg, spec_path, exc), err=True)
            fail += 1
            continue
        bumped = bump_spec_to_version(text, latest)
        if bumped is None:
            click.echo("%s  cannot find a literal Version in %s" % (pkg, spec_path), err=True)
            fail += 1
            continue
        new_text, old, new = bumped
        if old == new:
            click.echo("%s  already %s" % (pkg, old))
            skip += 1
            continue
        if new_text == text:
            click.echo("%s  %s -> %s but spec text unchanged" % (pkg, old, new), err=True)
            fail += 1
            continue
        spec_path.write_text(new_text, encoding="utf-8")
        click.echo("%s  %s -> %s  %s" % (pkg, old, new, spec_path))
        ok += 1
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
        existing = find_package_by_name(api_base, token, pkg)
        if existing:
            body: dict[str, Any] = {
                "package_id": int(existing["id"]),
                "release_id": rid,
                "architectures": arches,
                "skip_complete_arches": skip_complete,
            }
        else:
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
    """Run an explicit Koji-style chain: 'a b : c : d e'."""
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


@cli.command("layer-build")
@click.argument("spec_parts", nargs=-1, required=True)
@click.option("--release", "release_ref", required=True, help="Release id or releasename (e.g. oreon-11-rp1)")
@click.option("--side-tag", "side_tag_ref", required=True, help="Side tag id to publish into")
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
    help="Directory for per-job logs for immediately-queued jobs only",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Poll until each immediately-queued job reaches a terminal state",
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
@click.option(
    "--skip-already-built/--no-skip-already-built",
    default=False,
    help="Omit packages already at distgit EVR in the side tag and linked repos",
)
def cmd_layer_build(
    spec_parts: tuple[str, ...],
    release_ref: str,
    side_tag_ref: str,
    arch: str,
    log_dir: str | None,
    wait: bool,
    distgit_url: str,
    distgit_branch: str,
    skip_already_built: bool,
) -> None:
    """Queue root package(s); dependencies are discovered and built reactively."""
    token = require_auth(get_token())
    require_maintainer(token)
    api_base = get_api_url()
    rid = resolve_release_id(api_base, token, release_ref)
    packages = [p.strip() for p in spec_parts if p.strip()]
    if not packages:
        click.echo("No package names given.", err=True)
        sys.exit(2)
    arches = [a.strip() for a in arch.split(",") if a.strip()]
    log_path = Path(log_dir) if log_dir else None
    if log_path:
        log_path.mkdir(parents=True, exist_ok=True)

    body: dict[str, Any] = {
        "packages": packages,
        "release_id": rid,
        "side_tag_id": int(side_tag_ref),
        "architectures": arches,
        "distgit_url": distgit_url,
        "distgit_branch": distgit_branch,
        "skip_already_built": bool(skip_already_built),
    }
    data = http_json("POST", api_base, "/api/builds/layer-trigger", token=token, json_body=body, timeout=300.0)
    if not isinstance(data, dict):
        click.echo("Unexpected layer-trigger response", err=True)
        sys.exit(1)
    lid = data.get("layer_plan_id")
    click.echo("layer_plan_id=%s status=%s" % (lid, data.get("layer_plan_status") or ""))
    click.echo(data.get("message") or "Layer accepted.")
    jobs = data.get("jobs") or []
    if not isinstance(jobs, list):
        jobs = []
    jobs_out: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        jid = int(job["id"])
        click.echo(
            "job %s arch=%s status=%s layer_index=%s"
            % (
                jid,
                job.get("architecture") or "",
                job.get("status") or "",
                job.get("layer_index") if job.get("layer_index") is not None else "",
            )
        )
        if job.get("status") == "queued":
            jobs_out.append(job)

    need_poll = wait or log_path is not None
    if need_poll and jobs_out:
        for job in jobs_out:
            _wait_job_and_maybe_log(api_base, token, int(job["id"]), log_path)

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


def _list_all_layer_jobs(
    api_base: str,
    token: str | None,
    layer_id: int,
    status: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    limit = 1000
    while True:
        out = http_json(
            "GET",
            api_base,
            "/api/builds",
            token=token,
            params={
                "layer_plan_id": layer_id,
                "status": status,
                "limit": limit,
                "offset": offset,
            },
        )
        page = out.get("items") or []
        if not isinstance(page, list):
            break
        items.extend(j for j in page if isinstance(j, dict))
        total = int(out.get("total") or 0)
        offset += len(page)
        if not page or offset >= total:
            break
    return items


def _safe_log_name(value: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._+-]+", "_", (value or "").strip())
    return s.strip("._") or "unknown"

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
        existing = find_package_by_name(api_base, token, pkg)
        if existing:
            body: dict[str, Any] = {
                "package_id": int(existing["id"]),
                "release_id": rid,
                "architectures": arcs,
                "skip_complete_arches": True,
            }
        else:
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

@grp_logs.command("layer-fail")
@click.argument("layer_id", type=int)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(),
    default=None,
    help="Directory to write logs into (default ./layer-<id>-failed-logs)",
)
def logs_layer_fail(layer_id: int, out_dir: str | None) -> None:
    token = get_token()
    api_base = get_api_url()
    dest = Path(out_dir) if out_dir else Path("layer-%s-failed-logs" % layer_id)
    dest.mkdir(parents=True, exist_ok=True)
    jobs = _list_all_layer_jobs(api_base, token, layer_id, "failed")
    if not jobs:
        click.echo("no failed jobs in layer %s" % layer_id)
        return
    wrote = 0
    missing = 0
    for job in jobs:
        jid = int(job["id"])
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
    click.echo("layer %s failed jobs=%s logs written=%s missing=%s dir=%s" % (
        layer_id,
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
