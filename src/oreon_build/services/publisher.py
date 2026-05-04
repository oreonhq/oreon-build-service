"""
Repository publishing: stage RPMs from R2 under a persistent tree in ``~/.oreon/repo-tmp``,
run ``createrepo_c``, upload only ``repodata/`` to R2.

The local stage is reused across calls (no fresh temp dir per run). RPMs are only
downloaded when missing or when the size no longer matches R2.
"""

#
# Oreon Build Service - Publisher
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
from __future__ import annotations

import errno
import fcntl
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import List

from oreon_build.config import get_settings
from oreon_build.services.r2 import R2Client, get_r2_client
from oreon_build.services.signing import sign_repomd

logger = logging.getLogger(__name__)


_BUILDSYS_GROUP_PREFER: list[str] = [
    "bash",
    "coreutils",
    "cpio",
    "diffutils",
    "findutils",
    "gawk",
    "gcc",
    "gcc-c++",
    "grep",
    "gzip",
    "make",
    "patch",
    "redhat-rpm-config",
    "rpm-build",
    "sed",
    "tar",
    "unzip",
    "util-linux",
    "which",
    "xz",
]


def _rpm_name_from_filename(filename: str) -> str | None:
    base = filename.strip()
    if not base.endswith(".rpm") or base.endswith(".src.rpm"):
        return None
    base = base[:-4]
    parts = base.rsplit("-", 2)
    if len(parts) != 3:
        return None
    return parts[0] or None


def _discover_available_binary_names(rpms_dir: Path) -> list[str]:
    out: set[str] = set()
    for f in rpms_dir.iterdir():
        if not f.is_file():
            continue
        n = _rpm_name_from_filename(f.name)
        if n:
            out.add(n)
    return sorted(out)


def _fedora_comps_repo_path() -> Path:
    """
    Local clone path for Fedora comps templates.

    Defaults to `<repo>/vendor/fedora-comps` and can be overridden with
    `OREON_FEDORA_COMPS_REPO_PATH`.
    """
    raw = (os.environ.get("OREON_FEDORA_COMPS_REPO_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "vendor" / "fedora-comps"


def _select_local_fedora_comps_template(repo_path: Path) -> Path | None:
    """
    Pick a Fedora comps template from the local clone.

    Priority:
    - `OREON_FEDORA_COMPS_TEMPLATE_FILE` if set
    - highest `f/comps-f<NN>.xml.in` in the local clone
    """
    explicit = (os.environ.get("OREON_FEDORA_COMPS_TEMPLATE_FILE") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p
        logger.warning("OREON_FEDORA_COMPS_TEMPLATE_FILE does not exist: %s", p)
    best_num = -1
    best_path: Path | None = None
    for base in (repo_path, repo_path / "f"):
        if not base.is_dir():
            continue
        for p in base.iterdir():
            if not p.is_file():
                continue
            m = re.fullmatch(r"comps-f(\d+)\.xml\.in", p.name)
            if not m:
                continue
            n = int(m.group(1))
            if n > best_num:
                best_num = n
                best_path = p
    return best_path


def _load_fedora_comps_template_text() -> str | None:
    repo_path = _fedora_comps_repo_path()
    template_path = _select_local_fedora_comps_template(repo_path)
    if not template_path:
        logger.warning(
            "Fedora comps local clone not found or missing templates. Expected clone at: %s",
            repo_path,
        )
        return None
    try:
        body = template_path.read_text(encoding="utf-8")
        if "<comps" not in body:
            raise RuntimeError("local comps template is not valid XML")
        return body
    except Exception as exc:
        logger.warning("Could not read local Fedora comps template path=%s: %s", template_path, exc)
        return None


def _buildsys_group_xml(selected: list[str]) -> str:
    lines: list[str] = [
        "  <group>",
        "    <id>buildsys-build</id>",
        "    <name>buildsys-build</name>",
        "    <description>Buildroot package set for mock SRPM/RPM builds</description>",
        "    <default>false</default>",
        "    <uservisible>false</uservisible>",
        "    <packagelist>",
    ]
    for pkg in selected:
        safe = re.sub(r"[^A-Za-z0-9+_.-]", "", pkg)
        if not safe:
            continue
        lines.append(f'      <packagereq type="mandatory">{safe}</packagereq>')
    lines += [
        "    </packagelist>",
        "  </group>",
        "",
    ]
    return "\n".join(lines)


def _write_buildsys_comps(repo_dir: Path) -> Path | None:
    rpms_dir = repo_dir / "RPMS"
    if not rpms_dir.is_dir():
        return None
    available = _discover_available_binary_names(rpms_dir)
    if not available:
        return None

    selected = [n for n in _BUILDSYS_GROUP_PREFER if n in set(available)]
    # Ensure group is not empty. If preferred set is absent, seed a tiny fallback from repo content.
    if not selected:
        selected = available[: min(8, len(available))]

    # Start from Fedora comps template when available.
    template = _load_fedora_comps_template_text()
    if template and "<comps" in template:
        lines = [template.rstrip(), ""]
        if "<id>buildsys-build</id>" not in template:
            group = _buildsys_group_xml(selected)
            if "</comps>" in template:
                lines = [template.replace("</comps>", group + "</comps>").rstrip(), ""]
            else:
                lines = [template.rstrip(), "<comps>", group, "</comps>", ""]
    else:
        # Fallback minimal comps (still provides buildsys-build).
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<comps>",
            _buildsys_group_xml(selected).rstrip(),
            "</comps>",
            "",
        ]
    out = repo_dir / ".oreon-comps.xml"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _progress_stdout_enabled() -> bool:
    v = (os.environ.get("OREON_REPO_PROGRESS_STDOUT") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _progress(msg: str) -> None:
    """Emit lines for migrate-rpmrepo-metadata.sh (OREON_REPO_PROGRESS_STDOUT=1)."""
    if _progress_stdout_enabled():
        print(f"PROGRESS {msg}", flush=True)


class RepodataDiskQuotaError(RuntimeError):
    """Createrepo temp workspace hit EDQUOT or ENOSPC. Override OREON_REPO_TMPDIR or oreon_repo_tmpdir (default is ~/.oreon/repo-tmp)."""


def _repo_temp_dir_label(tmp_parent: str | None) -> str:
    """Human-readable path for logs (matches tempfile parent when set, else system tmp)."""
    if tmp_parent:
        return tmp_parent
    return os.environ.get("TMPDIR") or "/tmp"


def _is_disk_quota_or_no_space(err: OSError) -> bool:
    en = getattr(err, "errno", None)
    if en is None:
        return False
    if en == getattr(errno, "ENOSPC", 28):
        return True
    return en == getattr(errno, "EDQUOT", 122)


def _ensure_dir_parent(path: Path) -> str | None:
    """Create path if needed and return it as str, or None if unusable."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    if path.is_dir():
        return str(path)
    return None


def _repo_temp_dir_parent() -> str | None:
    """
    Parent directory for createrepo temp dirs (oreon-repo-*/repo/RPMS).

    Order: OREON_REPO_TMPDIR, then settings.oreon_repo_tmpdir, else ~/.oreon/repo-tmp
    (real disk under $HOME, not tmpfs /tmp).
    """
    raw = (os.environ.get("OREON_REPO_TMPDIR") or "").strip()
    candidates: list[str] = []
    if raw:
        candidates.append(raw)
    try:
        s = get_settings()
        cfg = (getattr(s, "oreon_repo_tmpdir", None) or "").strip()
        if cfg and cfg not in candidates:
            candidates.append(cfg)
    except Exception:
        pass
    for c in candidates:
        p = Path(c).expanduser()
        resolved = _ensure_dir_parent(p)
        if resolved:
            return resolved
    try:
        home_default = Path.home() / ".oreon" / "repo-tmp"
        resolved = _ensure_dir_parent(home_default)
        if resolved:
            return resolved
    except OSError:
        pass
    return None


def _stage_container_for_prefix(r2_prefix: str) -> Path:
    """Stable directory for one repo prefix (holds ``repo/`` and lock)."""
    parent = _repo_temp_dir_parent()
    if not parent:
        parent = str(Path.home() / ".oreon" / "repo-tmp")
    h = hashlib.sha256(r2_prefix.strip().encode()).hexdigest()[:24]
    return Path(parent) / "stages" / h


def _persistent_repo_root(r2_prefix: str) -> Path:
    """Local createrepo root: ``.../stages/<hash>/repo`` (RPMS + repodata under it)."""
    return _stage_container_for_prefix(r2_prefix) / "repo"


def _ensure_stage_sidecar(container: Path, r2_prefix: str) -> None:
    p = container / ".r2_prefix.txt"
    if p.exists():
        return
    try:
        p.write_text(r2_prefix.strip() + "\n", encoding="utf-8")
    except OSError:
        pass


@contextmanager
def _repo_stage_lock(container: Path) -> object:
    """Exclusive lock so two processes do not run createrepo on the same stage."""
    container.mkdir(parents=True, exist_ok=True)
    lock_path = container / ".oreon-repodata.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def _global_repogen_lock() -> object:
    """
    Cross-process global lock for createrepo_c runs.

    One repogen at a time prevents CPU/disk storms when many publish tasks
    land together. Per-prefix stage locking still prevents same-prefix races.
    """
    parent = _repo_temp_dir_parent()
    if not parent:
        parent = str(Path.home() / ".oreon" / "repo-tmp")
    lock_dir = Path(parent)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".oreon-global-repogen.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _createrepo_workers() -> int:
    raw = (os.environ.get("OREON_CREATEREPO_WORKERS") or "50").strip()
    try:
        w = int(raw)
    except ValueError:
        w = 50
    return max(1, min(w, 128))


def _createrepo_timeout_sec() -> int:
    raw = (os.environ.get("OREON_CREATEREPO_TIMEOUT_SEC") or "1800").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 1800
    return max(60, min(n, 8 * 3600))


def _r2_etag_strong_content_md5(etag: str) -> bool:
    """
    S3 / R2 single-part object ETag is the MD5 hex. Multipart and other forms are not; do not
    use those for a local file comparison.
    """
    t = (etag or "").strip()
    if not t or "-" in t:
        return False
    if len(t) != 32:
        return False
    t = t.lower()
    for c in t:
        if c not in "0123456789abcdef":
            return False
    return True


def _local_rpm_bytes_match_r2_etag(path: Path, etag: str) -> bool:
    """True if the file's MD5 matches a strong (single-part) S3 ETag."""
    if not _r2_etag_strong_content_md5(etag) or not path.is_file() or path.stat().st_size == 0:
        return False
    m = hashlib.md5()
    m.update(path.read_bytes())
    return m.hexdigest() == etag.lower()


def _etag_cache_path(container: Path) -> Path:
    return container / ".etag-cache.json"


def _load_etag_cache(container: Path) -> dict[str, str]:
    """Load the persistent filename->etag map from the container directory."""
    import json

    p = _etag_cache_path(container)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
    return {}


def _save_etag_cache(container: Path, cache: dict[str, str]) -> None:
    import json

    p = _etag_cache_path(container)
    try:
        tmp = p.parent / (p.name + ".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(p)
    except Exception as exc:
        logger.debug("Could not save etag cache to %s: %s", p, exc)


def _sync_rpms_dir_from_r2(
    client: R2Client,
    rpms_prefix: str,
    rpms_dir: Path,
    *,
    container: Path | None = None,
) -> int:
    """
    Ensure ``rpms_dir`` matches R2 under ``rpms_prefix``.

    R2 is the source of truth. We verify each local file against the R2 ETag:
    - If a persistent ``container`` is provided, a JSON ETag cache is used so that files
      already verified in a previous run are trusted without re-reading from disk. This
      avoids reading tens of GB on every publish for repos with thousands of packages.
    - On cache miss (first run or new files), MD5 is computed from disk and the result is
      stored in the cache for next time.
    - Files whose ETag is absent or multipart are accepted by size alone.
    """
    import json as _json

    rpms_dir.mkdir(parents=True, exist_ok=True)
    objs = client.list_objects(rpms_prefix)
    wanted: dict[str, tuple[str, int, str]] = {}
    for o in objs:
        key = o.get("key")
        if not key:
            continue
        base = key.split("/")[-1]
        if not base.endswith(".rpm"):
            continue
        sz = int(o.get("size") or 0)
        etag = (o.get("etag") or "").strip()
        if base in wanted:
            prev_key, _, _ = wanted[base]
            logger.warning(
                "Duplicate RPM basename in R2 listing, last wins: base=%s was_key=%s now_key=%s",
                base,
                prev_key,
                key,
            )
        wanted[base] = (key, sz, etag)

    etag_cache: dict[str, str] = _load_etag_cache(container) if container else {}
    cache_dirty = False

    for f in list(rpms_dir.iterdir()):
        if f.is_file() and f.suffix == ".rpm" and f.name not in wanted:
            try:
                f.unlink()
            except OSError as e:
                logger.warning("Could not remove stale local RPM %s: %s", f, e)
            etag_cache.pop(f.name, None)
            cache_dirty = True

    fetched = 0
    for fname, (key, sz, etag) in wanted.items():
        dest = rpms_dir / fname
        need_fetch = True
        if sz > 0 and dest.is_file() and dest.stat().st_size == sz:
            if etag and _r2_etag_strong_content_md5(etag):
                cached_etag = etag_cache.get(fname)
                if cached_etag == etag:
                    # Already verified against this exact ETag - no disk read needed.
                    need_fetch = False
                elif _local_rpm_bytes_match_r2_etag(dest, etag):
                    need_fetch = False
                    etag_cache[fname] = etag
                    cache_dirty = True
                # size matches but ETag differs: re-download (content changed in R2)
            else:
                # No strong ETag from R2: trust size match (R2 always sends etag but be safe).
                need_fetch = False

        if not need_fetch:
            continue
        data = client.get_object(key)
        if not data:
            raise RuntimeError(f"missing or empty RPM object at {key!r}, aborting repodata update")
        dest.write_bytes(data)
        fetched += 1
        if etag and _r2_etag_strong_content_md5(etag):
            etag_cache[fname] = etag
            cache_dirty = True

    if cache_dirty and container:
        _save_etag_cache(container, etag_cache)

    if fetched:
        logger.info("Fetched %d new/changed RPMs from R2 under %s", fetched, rpms_prefix)

    return len(wanted)


def _list_repodata_keys(client: R2Client, r2_prefix: str) -> list[str]:
    repodata_prefix = f"{r2_prefix}/repodata/"
    objs = client.list_objects(repodata_prefix)
    keys: list[str] = []
    for o in objs:
        k = o.get("key")
        if k:
            keys.append(k)
    return keys


def _cleanup_old_repodata(
    client: R2Client, r2_prefix: str, keep_filenames: set[str]
) -> None:
    """
    Remove stale objects under `<r2_prefix>/repodata/` that are not part of the
    newly generated repodata set (prevents "repodata junk" accumulating).
    """
    old_keys = _list_repodata_keys(client, r2_prefix)
    if not old_keys:
        return

    to_delete: list[str] = []
    for k in old_keys:
        filename = k.split("/")[-1]
        if filename not in keep_filenames:
            to_delete.append(k)

    if not to_delete:
        return

    for k in to_delete:
        client.delete_object(k)

    logger.info(
        "Purged %d stale repodata objects from %s",
        len(to_delete),
        f"{r2_prefix}/repodata/",
    )


def _createrepo_cache_dir(container: Path) -> Path:
    """Persistent per-prefix checksum cache for createrepo_c --cachedir across runs."""
    d = container / ".createrepo-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_createrepo(repo_dir: Path, *, container: Path | None = None) -> None:
    # Never use capture_output/PIPE here. createrepo_c can write a lot to stderr; a full pipe blocks the child
    # while the parent waits forever (looks like a hang with RPMs already on disk).
    #
    # When container is provided (persistent stage) we pass --update so only new/changed RPMs get
    # re-checksummed. Without it every publish re-scans the entire repo which is extremely slow once
    # there are hundreds of packages. --cachedir stores per-file checksums across API restarts so
    # even after a service restart the next run is fast.
    def _cleanup_tmp_repodata() -> None:
        tmp_repodata = repo_dir / ".repodata"
        if tmp_repodata.exists():
            # Previous interrupted createrepo runs can leave this behind and every future run
            # fails with "Temporary repodata directory ... already exists".
            try:
                shutil.rmtree(tmp_repodata)
                logger.warning("Removed stale createrepo temp dir %s", tmp_repodata)
            except OSError as e:
                logger.warning("Could not remove stale createrepo temp dir %s: %s", tmp_repodata, e)

    def _run_once(workers: int) -> None:
        timeout_s = _createrepo_timeout_sec()
        comps_file = _write_buildsys_comps(repo_dir)
        use_update = container is not None and (repo_dir / "repodata" / "repomd.xml").exists()
        logger.info(
            "createrepo_c starting repo_root=%s workers=%s update=%s",
            repo_dir,
            workers,
            use_update,
        )
        cmd = [
            "createrepo_c",
            str(repo_dir),
            f"--workers={workers}",
        ]
        if use_update:
            cmd.append("--update")
        if container is not None:
            try:
                cache_dir = _createrepo_cache_dir(container)
                cmd += ["--cachedir", str(cache_dir)]
            except OSError:
                pass
        if comps_file is not None and comps_file.exists():
            cmd += ["--groupfile", str(comps_file)]
        try:
            subprocess.run(
                cmd,
                check=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"createrepo_c timed out after {timeout_s}s for repo_root={repo_dir} workers={workers}"
            ) from e
        repomd = repo_dir / "repodata" / "repomd.xml"
        if not repomd.exists():
            raise RuntimeError(f"createrepo_c reported success but {repomd} is missing")
        logger.info("createrepo_c finished repo_root=%s workers=%s update=%s", repo_dir, workers, use_update)

    _cleanup_tmp_repodata()
    w = _createrepo_workers()
    try:
        _run_once(w)
    except Exception:
        fb_raw = (os.environ.get("OREON_CREATEREPO_FALLBACK_WORKERS") or "8").strip()
        try:
            fb = int(fb_raw)
        except ValueError:
            fb = 8
        fb = max(1, min(fb, 32))
        if fb == w:
            raise
        logger.exception(
            "createrepo_c failed with workers=%s, retrying once with workers=%s after cleanup",
            w,
            fb,
        )
        _cleanup_tmp_repodata()
        _run_once(fb)


def _upload_repodata_dir_ordered(client: R2Client, r2_prefix: str, repodata_dir: Path) -> None:
    """
    Upload repodata so repomd.xml (and its signature) go last. Otherwise a client can
    fetch a new repomd that references primary/filelists objects not visible yet.
    """
    files = [f for f in repodata_dir.iterdir() if f.is_file()]
    by_name = {f.name: f for f in files}
    tail: list[str] = []
    if "repomd.xml" in by_name:
        tail.append("repomd.xml")
    if "repomd.xml.asc" in by_name:
        tail.append("repomd.xml.asc")
    head = sorted(n for n in by_name if n not in tail)
    for name in head + tail:
        f = by_name[name]
        r2_key = f"{r2_prefix}/repodata/{name}"
        client.put_object(r2_key, f.read_bytes())


def update_repodata_at_prefix(r2_prefix: str, r2_client: R2Client | None = None) -> bool:
    """
    RPMs are already at r2_prefix/RPMS/. Download them, run createrepo_c, upload only repodata.
    Use this when the worker (or other path) has already uploaded RPMs directly to the repo.
    """
    client = r2_client or get_r2_client()
    rpms_prefix = f"{r2_prefix}/RPMS/"
    objs = client.list_objects(rpms_prefix)
    rpm_keys = [
        o["key"]
        for o in objs
        if o.get("key") and o["key"].split("/")[-1].lower().endswith(".rpm")
    ]
    if not rpm_keys:
        logger.warning("No RPMs at %s, skipping createrepo", rpms_prefix)
        return False
    _progress(f"{r2_prefix} listed {len(rpm_keys)} RPM keys from R2")
    tmp_parent = _repo_temp_dir_parent()
    container = _stage_container_for_prefix(r2_prefix)
    _ensure_stage_sidecar(container, r2_prefix)
    repo_root = _persistent_repo_root(r2_prefix)
    rpms_dir = repo_root / "RPMS"
    try:
        with _repo_stage_lock(container):
            _progress(f"{r2_prefix} syncing RPMs to local stage (may take a while)")
            n = _sync_rpms_dir_from_r2(client, rpms_prefix, rpms_dir, container=container)
            _progress(f"{r2_prefix} synced {n} RPMs, running createrepo_c")
            _run_createrepo(repo_root, container=container)
            _progress(f"{r2_prefix} createrepo_c done, uploading repodata to R2")
            repodata_dir = repo_root / "repodata"
            if repodata_dir.exists():
                # Keep track of which repodata filenames we just generated so we can
                # delete older R2 objects that createrepo no longer produces.
                new_filenames = {f.name for f in repodata_dir.iterdir()}

                repomd = repodata_dir / "repomd.xml"
                if repomd.exists():
                    sign_repomd(repomd)

                # Upload new repodata first; only after that do we purge stale objects.
                _upload_repodata_dir_ordered(client, r2_prefix, repodata_dir)

                _cleanup_old_repodata(client, r2_prefix, new_filenames)
                logger.info("Updated repodata at R2 prefix %s (%s RPMs in metadata)", r2_prefix, n)
                return True
            logger.error("createrepo_c produced no repodata under %s", repo_root)
            return False
    except OSError as e:
        if _is_disk_quota_or_no_space(e):
            label = _repo_temp_dir_label(tmp_parent)
            msg = (
                f"disk quota or no space while composing repo metadata under {label}. "
                "Set OREON_REPO_TMPDIR or oreon_repo_tmpdir to a larger path (default is ~/.oreon/repo-tmp)."
            )
            logger.error(msg)
            raise RepodataDiskQuotaError(msg) from e
        raise


def publish_rpms_to_r2(
    r2_prefix: str,
    rpm_keys: List[str],
    r2_client: R2Client | None = None,
) -> None:
    """
    Given a list of R2 keys where RPMs already live, create repodata and upload to R2.

    Puts each object at ``<r2_prefix>/RPMS/<name>`` (when it is not already there) *before* running
    createrepo or uploading repodata, so a client can never see new primary/repomd checksums that
    still point at stale bytes in ``RPMS/``.
    """
    client = r2_client or get_r2_client()
    tmp_parent = _repo_temp_dir_parent()
    try:
        with tempfile.TemporaryDirectory(prefix="oreon-repo-", dir=tmp_parent) as tmp:
            repo_root = Path(tmp) / "repo"
            rpms_dir = repo_root / "RPMS"
            rpms_dir.mkdir(parents=True)
            prefix = r2_prefix.rstrip("/")
            for key in rpm_keys:
                data = client.get_object(key)
                if not data:
                    raise RuntimeError(f"missing or empty RPM object at {key!r}, aborting publish")
                fname = key.split("/")[-1]
                dest = rpms_dir / fname
                if dest.exists():
                    logger.warning(
                        "Duplicate RPM basename in publish list, overwriting %s (key %s)",
                        fname,
                        key,
                    )
                dest.write_bytes(data)
                canonical = f"{prefix}/RPMS/{fname}"
                if key != canonical:
                    client.put_object(canonical, data)
            _run_createrepo(repo_root)
            repodata_dir = repo_root / "repodata"
            if repodata_dir.exists():
                new_filenames = {f.name for f in repodata_dir.iterdir()}
                _upload_repodata_dir_ordered(client, r2_prefix, repodata_dir)

                _cleanup_old_repodata(client, r2_prefix, new_filenames)
    except OSError as e:
        if _is_disk_quota_or_no_space(e):
            label = _repo_temp_dir_label(tmp_parent)
            msg = (
                f"disk quota or no space while publishing repo under {label}. "
                "Set OREON_REPO_TMPDIR or oreon_repo_tmpdir to a larger path (default is ~/.oreon/repo-tmp)."
            )
            logger.error(msg)
            raise RepodataDiskQuotaError(msg) from e
        raise
    logger.info("Published repo to R2 prefix %s", r2_prefix)
