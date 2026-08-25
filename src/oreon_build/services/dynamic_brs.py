"""Evaluate BuildRequires the way rpm/mock do, including %generate_buildrequires."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

_RE_GENERATE = re.compile(r"^\s*%generate_buildrequires\b", re.IGNORECASE | re.MULTILINE)
_RE_SOURCE = re.compile(r"^Source\d*\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_RE_PATCH = re.compile(r"^Patch\d*\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_RE_RPMLIB = re.compile(r"^rpmlib\(", re.IGNORECASE)
_RE_CARGO_SPEC = re.compile(
    r"^\s*%\s*(?:cargo_prep|cargo_build|cargo_install|cargo_test|cargo_generate_buildrequires)\b"
    r"|^\s*%\s*global\s+crate\b"
    r"|%\{crates_source\}"
    r"|^\s*%\s*cargo_generate_buildrequires\b",
    re.IGNORECASE | re.MULTILINE,
)
_CACHE: dict[str, tuple[float, list[str], bool]] = {}
_CACHE_TTL = 1800.0
_DYNAMIC_BR_EVAL_VERSION = "3"


def _scratch_base_dir() -> str | None:
    """Prefer tmpfs for the rpmbuild -br scratch tree.

    Unpack+patch+%generate_buildrequires is real disk I/O for every package
    that goes through this path. /dev/shm is memory-backed, so putting the
    scratch tree there turns that I/O into memory copies. Falls back to the
    default tempdir (disk) if /dev/shm isn't there or doesn't have room.
    """
    raw = (os.getenv("OREON_BR_SCRATCH_DIR") or "").strip()
    candidates = [raw] if raw else ["/dev/shm"]
    for cand in candidates:
        if not cand:
            continue
        try:
            st = os.statvfs(cand)
            free = st.f_bavail * st.f_frsize
            if free > 512 * 1024 * 1024 and os.access(cand, os.W_OK):
                return cand
        except OSError:
            continue
    return None


@dataclass
class DynBrResult:
    buildrequires: list[str]
    complete: bool
    error: str = ""


def spec_has_generate_buildrequires(spec_text: str) -> bool:
    return bool(_RE_GENERATE.search(spec_text or ""))


def spec_uses_cargo_generate_buildrequires(spec_text: str) -> bool:
    """Rust/cargo specs need buildreqs.nosrc crate() deps. static rpmspec BRs lie."""
    return bool(_RE_CARGO_SPEC.search(spec_text or ""))


def _rpmbuild_env() -> dict[str, str]:
    env = dict(os.environ)
    home = Path.home()
    extras = [str(home / ".cargo" / "bin"), str(home / ".local" / "bin")]
    path = env.get("PATH") or ""
    for p in reversed(extras):
        if p and p not in path.split(os.pathsep):
            path = p + os.pathsep + path
    env["PATH"] = path
    return env


def _define_args(defines: dict[str, str] | None) -> list[str]:
    from oreon_build.core.spec_bcond import rpm_bcond_define_args

    return rpm_bcond_define_args(defines)


def _run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_rpmbuild_env(),
    )


def rpmspec_query_buildrequires(
    spec_text: str,
    *,
    defines: dict[str, str] | None = None,
) -> list[str]:
    """Static/macro-expanded BRs via rpmspec (no %generate_buildrequires pass)."""
    with tempfile.TemporaryDirectory(prefix="oreon-rpmspec-") as td:
        spec_path = Path(td) / "pkg.spec"
        spec_path.write_text(spec_text or "", encoding="utf-8")
        cmd = ["rpmspec", "-q", "--buildrequires", str(spec_path)]
        cmd.extend(_define_args(defines))
        proc = _run(cmd, timeout=60)
        if proc.returncode != 0:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for line in (proc.stdout or "").splitlines():
            br = (line or "").strip()
            if not br or _RE_RPMLIB.match(br) or br in seen:
                continue
            seen.add(br)
            out.append(br)
        return out


def _entries_from_blob(
    blob: str, tag_re: re.Pattern[str]
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in tag_re.finditer(blob):
        raw = (m.group(1) or "").strip()
        if not raw:
            continue
        url = ""
        name = raw
        if raw.startswith(("http://", "https://", "ftp://")):
            url = raw
            if "#/" in raw:
                name = raw.rsplit("#/", 1)[-1]
            else:
                name = raw.rstrip("/").rsplit("/", 1)[-1] or "Source"
        name = name.split("?", 1)[0].strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append((name, url))
    return out


_TAG_ENTRIES_CACHE: dict[str, tuple[list[tuple[str, str]], list[tuple[str, str]]]] = {}
_TAG_ENTRIES_CACHE_MAX = 512


def _tag_entries_cache_key(spec_text: str, defines: dict[str, str] | None) -> str:
    h = hashlib.sha256()
    h.update((spec_text or "").encode("utf-8", errors="replace"))
    for k in sorted((defines or {}).keys()):
        h.update(f"{k}={(defines or {})[k]}\n".encode())
    return h.hexdigest()


def _source_and_patch_entries(
    spec_text: str, defines: dict[str, str] | None
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """One rpmspec -P call for both Source*/Patch* tags (not one call each).

    rpmspec -P re-expands the whole spec through the macro engine; every
    extra invocation is a real subprocess fork+exec, not free. Called
    multiple times per package (locals, url sources) in the BR pipeline, so
    this is memoized per (spec_text, defines) within the process, avoiding
    duplicate spawns for the exact same macro-expansion work.
    """
    key = _tag_entries_cache_key(spec_text, defines)
    hit = _TAG_ENTRIES_CACHE.get(key)
    if hit is not None:
        return hit
    with tempfile.TemporaryDirectory(prefix="oreon-rpmspec-src-") as td:
        spec_path = Path(td) / "pkg.spec"
        spec_path.write_text(spec_text or "", encoding="utf-8")
        cmd = ["rpmspec", "-P", str(spec_path)]
        cmd.extend(_define_args(defines))
        proc = _run(cmd, timeout=60)
        blob = proc.stdout or ""
    result = (_entries_from_blob(blob, _RE_SOURCE), _entries_from_blob(blob, _RE_PATCH))
    if len(_TAG_ENTRIES_CACHE) >= _TAG_ENTRIES_CACHE_MAX:
        _TAG_ENTRIES_CACHE.clear()
    _TAG_ENTRIES_CACHE[key] = result
    return result


def _parse_source_entries(spec_text: str, defines: dict[str, str] | None) -> list[tuple[str, str]]:
    """Return (basename, url_or_empty) for each SourceN after macro expand."""
    return _source_and_patch_entries(spec_text, defines)[0]


def _parse_patch_entries(spec_text: str, defines: dict[str, str] | None) -> list[tuple[str, str]]:
    """Return (basename, url_or_empty) for each PatchN after macro expand."""
    return _source_and_patch_entries(spec_text, defines)[1]


def _parse_source_urls(spec_text: str, defines: dict[str, str] | None) -> list[str]:
    return [url for _name, url in _parse_source_entries(spec_text, defines) if url]


def local_source_basenames(spec_text: str, defines: dict[str, str] | None = None) -> list[str]:
    """Local SourceN + PatchN files that live beside the spec in distgit."""
    names: list[str] = []
    seen: set[str] = set()
    sources, patches = _source_and_patch_entries(spec_text, defines)
    for name, url in (*sources, *patches):
        if url or not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def clear_tag_entries_cache() -> None:
    _TAG_ENTRIES_CACHE.clear()


def _parse_source0_url(spec_text: str, defines: dict[str, str] | None) -> str | None:
    urls = _parse_source_urls(spec_text, defines)
    return urls[0] if urls else None


def _crates_io_static_url(url: str) -> str | None:
    """Rewrite crates.io API download URLs to static.crates.io (avoids API 429)."""
    raw = (url or "").strip()
    if not raw:
        return None
    # https://crates.io/api/v1/crates/<name>/<ver>/download#/<name>-<ver>.crate
    m = re.match(
        r"^https?://crates\.io/api/v1/crates/([^/]+)/([^/]+)/download(?:#/.*)?$",
        raw,
        re.IGNORECASE,
    )
    if not m:
        return None
    name, ver = m.group(1), m.group(2)
    return f"https://static.crates.io/crates/{name}/{name}-{ver}.crate"

def _http_download(url: str, dest: Path, *, timeout: int = 180) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    urls = [url]
    alt = _crates_io_static_url(url)
    if alt and alt not in urls:
        # prefer static CDN first for crates.io
        urls = [alt, url]
    last_err: Exception | None = None
    for u in urls:
        try:
            req = urllib.request.Request(
                u, headers={"User-Agent": "oreon-build-service/dynamic-brs"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
                shutil.copyfileobj(resp, out)
            tmp.replace(dest)
            return
        except Exception as exc:
            last_err = exc
            continue
    if last_err:
        raise last_err
    raise RuntimeError(f"download failed for {url}")


def _cache_key(spec_text: str, defines: dict[str, str] | None, local_sources: Iterable[str]) -> str:
    h = hashlib.sha256()
    h.update((spec_text or "").encode("utf-8", errors="replace"))
    for k in sorted((defines or {}).keys()):
        h.update(f"{k}={(defines or {})[k]}\n".encode())
    for p in sorted(local_sources):
        h.update(p.encode())
        try:
            h.update(Path(p).read_bytes()[:65536])
        except Exception:
            pass
    return h.hexdigest()


def _requires_from_nosrc_rpm(path: Path) -> list[str]:
    """Read Requires from a buildreqs.nosrc.rpm via python-rpm header API."""
    try:
        import rpm
    except ImportError:
        return _requires_from_nosrc_rpm_cli(path)

    ts = rpm.TransactionSet()
    try:
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
    except Exception:
        pass
    fdno = -1
    try:
        fdno = os.open(str(path), os.O_RDONLY)
        hdr = ts.hdrFromFdno(fdno)
    except Exception:
        logger.warning("python-rpm failed reading %s, falling back to rpm -qp", path, exc_info=True)
        return _requires_from_nosrc_rpm_cli(path)
    finally:
        if fdno >= 0:
            try:
                os.close(fdno)
            except Exception:
                pass

    out: list[str] = []
    seen: set[str] = set()
    try:
        for dep in rpm.ds(hdr, "requires"):
            raw = (dep.DNEVR() or "").strip()
            if raw.startswith("R "):
                raw = raw[2:].strip()
            br = raw.strip()
            if not br or _RE_RPMLIB.match(br) or br in seen:
                continue
            seen.add(br)
            out.append(br)
    except Exception:
        logger.warning("rpm.ds requires failed for %s", path, exc_info=True)
        return _requires_from_nosrc_rpm_cli(path)
    return out


def _requires_from_nosrc_rpm_cli(path: Path) -> list[str]:
    q = _run(["rpm", "-qp", "--requires", str(path)], timeout=30)
    out: list[str] = []
    seen: set[str] = set()
    for line in (q.stdout or "").splitlines():
        br = (line or "").strip()
        if not br or _RE_RPMLIB.match(br) or br in seen:
            continue
        seen.add(br)
        out.append(br)
    return out


def _http_get_bytes(url: str, *, timeout: int = 60) -> bytes:
    urls = [url]
    alt = _crates_io_static_url(url)
    if alt and alt not in urls:
        urls = [alt, url]
    last_err: Exception | None = None
    for u in urls:
        try:
            req = urllib.request.Request(
                u, headers={"User-Agent": "oreon-build-service/dynamic-brs"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            last_err = exc
            continue
    raise last_err or RuntimeError(f"download failed for {url}")


def _extract_cargo_toml_from_tarball(data: bytes, dest_dir: Path) -> Path | None:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            candidates = [m for m in tf.getmembers() if m.isfile() and m.name.endswith("/Cargo.toml")]
            if not candidates:
                candidates = [m for m in tf.getmembers() if m.isfile() and m.name == "Cargo.toml"]
            if not candidates:
                return None
            member = min(candidates, key=lambda m: m.name.count("/"))
            fobj = tf.extractfile(member)
            if fobj is None:
                return None
            out = dest_dir / "Cargo.toml"
            out.write_bytes(fobj.read())
            return out
    except Exception:
        logger.warning("cargo tarball Cargo.toml extraction failed", exc_info=True)
        return None


def _cargo2rpm_buildrequires(manifest: Path, *, with_check: bool = False) -> list[str] | None:
    cmd = ["cargo2rpm", "--path", str(manifest), "buildrequires"]
    if with_check:
        cmd.append("--with-check")
    proc = _run(cmd, timeout=30)
    if proc.returncode != 0:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        br = line.strip()
        if not br or br in seen:
            continue
        seen.add(br)
        out.append(br)
    return out


def _cargo_generator_flags(spec_text: str) -> str:
    """Raw text of the %cargo_generate_buildrequires invocation line.

    Upstream macro passes extra flags straight to cargo2rpm. A bare call is
    safe to mirror with cargo2rpm directly, anything carrying args is not.
    """
    m = re.search(
        r"^[ \t]*%cargo_generate_buildrequires([ \t]+\S.*)?$",
        (spec_text or ""),
        re.IGNORECASE | re.MULTILINE,
    )
    return (m.group(1) or "").strip() if m else ""


def _evaluate_cargo_buildrequires_fast(
    spec_text: str,
    *,
    defines: dict[str, str] | None,
    local_source_paths: list[Path] | None,
    target_arches: set[str] | None,
) -> DynBrResult | None:
    from oreon_build.core.spec_bcond import parse_spec_bconds
    from oreon_build.services.cargo_target_brs import filter_foreign_target_crate_brs

    if _cargo_generator_flags(spec_text):
        return None

    raw_check = str((defines or {}).get("with_check", "")).strip()
    if not raw_check:
        bcond = parse_spec_bconds(spec_text)
        raw_check = str(bcond.defaults.get("with_check", "0"))
    try:
        with_check = int(raw_check or "0") > 0
    except ValueError:
        with_check = False

    url = _parse_source0_url(spec_text, defines)
    data: bytes | None = None
    if url:
        try:
            data = _http_get_bytes(url)
        except Exception:
            data = None
    if data is None:
        sources, _patches = _source_and_patch_entries(spec_text, defines)
        if sources:
            name0 = sources[0][0]
            for p in local_source_paths or []:
                if p and p.name == name0 and p.is_file():
                    try:
                        data = p.read_bytes()
                    except Exception:
                        data = None
                    break
    if data is None:
        return None
    with tempfile.TemporaryDirectory(prefix="oreon-cargo2rpm-", dir=_scratch_base_dir()) as td:
        manifest = _extract_cargo_toml_from_tarball(data, Path(td))
        if manifest is None:
            return None
        brs = _cargo2rpm_buildrequires(manifest, with_check=with_check)
        if brs is None:
            return None
        brs = filter_foreign_target_crate_brs(
            brs, cargo_tomls=[manifest], arches=target_arches
        )
        return DynBrResult(buildrequires=brs, complete=True)


def evaluate_generate_buildrequires(
    spec_text: str,
    *,
    defines: dict[str, str] | None = None,
    local_source_paths: list[Path] | None = None,
    allow_source_download: bool = True,
    target_arches: set[str] | None = None,
    static_brs: list[str] | None = None,
) -> DynBrResult:
    """Run rpmbuild -br and read Requires from the buildreqs nosrc rpm.

    Same DynamicBuildRequires path mock uses before builddep.
    complete=False when %generate_buildrequires could not be evaluated.
    For cargo specs, crate() caps that only apply to foreign target cfgs
    (windows/hermit/redox/...) are dropped for the planned Linux arches.
    """
    import time

    from oreon_build.services.cargo_target_brs import (
        filter_foreign_target_crate_brs,
        find_cargo_tomls,
    )

    has_dynamic = spec_has_generate_buildrequires(spec_text) or spec_uses_cargo_generate_buildrequires(spec_text)
    if not has_dynamic:
        brs = rpmspec_query_buildrequires(spec_text, defines=defines)
        return DynBrResult(buildrequires=brs, complete=True)

    locals_ = [str(p) for p in (local_source_paths or []) if p and Path(p).is_file()]
    arch_key = ",".join(sorted((target_arches or set()) or {"x86_64"}))
    key = _cache_key(spec_text, {**(defines or {}), "__arches__": arch_key, "__v__": _DYNAMIC_BR_EVAL_VERSION}, locals_)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return DynBrResult(buildrequires=list(hit[1]), complete=bool(hit[2]))

    if spec_uses_cargo_generate_buildrequires(spec_text):
        fast = _evaluate_cargo_buildrequires_fast(
            spec_text,
            defines=defines,
            local_source_paths=local_source_paths,
            target_arches=target_arches,
        )
        if fast is not None:
            _CACHE[key] = (now, list(fast.buildrequires), fast.complete)
            return fast

    with tempfile.TemporaryDirectory(prefix="oreon-rpmbr-", dir=_scratch_base_dir()) as td:
        top = Path(td)
        for sub in ("BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS"):
            (top / sub).mkdir(parents=True, exist_ok=True)
        spec_path = top / "SPECS" / "pkg.spec"
        spec_path.write_text(spec_text or "", encoding="utf-8")
        for src in locals_:
            shutil.copy2(src, top / "SOURCES" / Path(src).name)

        source_fail = False
        if allow_source_download:
            for name, url in _parse_source_entries(spec_text, defines):
                if not url:
                    continue
                dest = top / "SOURCES" / name
                if dest.exists():
                    continue
                try:
                    _http_download(url, dest)
                except Exception:
                    source_fail = True
                    logger.warning("source download failed for dyn BRs: %s", url, exc_info=True)

        cmd = [
            "rpmbuild",
            "-br",
            "--nodeps",
            "--define",
            f"_topdir {top}",
            "--define",
            "_build_id_links none",
            "--define",
            "source_date_epoch_from_changelog 0",
        ]
        cmd.extend(_define_args(defines))
        cmd.append(str(spec_path))
        proc = _run(cmd, timeout=300)
        brs: list[str] = []
        nosrc = sorted((top / "SRPMS").glob("*.buildreqs.nosrc.rpm"))
        if nosrc:
            brs = _requires_from_nosrc_rpm(nosrc[-1])

        if brs:
            if spec_uses_cargo_generate_buildrequires(spec_text):
                brs = filter_foreign_target_crate_brs(
                    brs,
                    cargo_tomls=find_cargo_tomls(top / "BUILD"),
                    arches=target_arches,
                )
            # pyproject/%generate_buildrequires often exits non-zero after writing
            # the nosrc Requires (deps not in the empty buildroot). that output is
            # still the real DynamicBuildRequires list mock would use.
            _CACHE[key] = (now, list(brs), True)
            return DynBrResult(buildrequires=brs, complete=True)

        # caller (evaluate_package_brs) already computed this once; dont
        # fork+exec rpmspec a second time for the same spec+defines.
        static = (
            list(static_brs)
            if static_brs is not None
            else rpmspec_query_buildrequires(spec_text, defines=defines)
        )
        err = (proc.stderr or proc.stdout or "")[-400:].strip()
        if source_fail and not err:
            err = "source download failed"
        if not err:
            err = "rpmbuild -br produced no buildreqs nosrc"
        cargo = spec_uses_cargo_generate_buildrequires(spec_text)
        if cargo:
            # cargo second pass is rich crate() caps. static BRs alone are a lie.
            logger.info(
                "cargo rpmbuild -br produced no nosrc (complete=False): %s",
                err[:200],
            )
            _CACHE[key] = (now, list(static), False)
            return DynBrResult(
                buildrequires=static,
                complete=False,
                error=err or "cargo buildreqs nosrc missing",
            )
        # golang/py generate often needs tools from static BRs (go-vendor-tools,
        # hatchling, ...) that mock installs in builddep pass 1 inside the chroot.
        # on the planner host those tools may be missing, so -br writes no nosrc.
        # static BRs schedule those tools (pass-1). complete=False so planner can
        # re-run -br once tools are installable under the plan overlay.
        if static:
            logger.info(
                "rpmbuild -br produced no nosrc, static pass-1 BRs (%s): %s",
                len(static),
                err[:200],
            )
            _CACHE[key] = (now, list(static), False)
            return DynBrResult(buildrequires=static, complete=False, error=err)
        logger.info("rpmbuild -br incomplete for dyn BRs: %s", err[:200])
        _CACHE[key] = (now, [], False)
        return DynBrResult(buildrequires=[], complete=False, error=err)


def merge_buildrequires(*groups: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for br in group:
            b = (br or "").strip()
            if not b or b in seen or "%" in b:
                continue
            seen.add(b)
            out.append(b)
    return out


def clear_dynamic_br_cache() -> None:
    _CACHE.clear()
