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


def _sync_rpms_dir_from_r2(client: R2Client, rpms_prefix: str, rpms_dir: Path) -> int:
    """
    Ensure ``rpms_dir`` matches R2 under ``rpms_prefix``. Skip download if file exists with same size.
    Remove local ``*.rpm`` files that no longer exist in R2.
    Returns number of RPM objects listed in R2.
    """
    rpms_dir.mkdir(parents=True, exist_ok=True)
    objs = client.list_objects(rpms_prefix)
    wanted: dict[str, tuple[str, int]] = {}
    for o in objs:
        key = o.get("key")
        if not key:
            continue
        base = key.split("/")[-1]
        if not base.endswith(".rpm"):
            continue
        sz = int(o.get("size") or 0)
        if base in wanted:
            logger.warning(
                "Duplicate RPM basename in R2 listing, using last key base=%s key=%s",
                base,
                key,
            )
        wanted[base] = (key, sz)

    for f in list(rpms_dir.iterdir()):
        if f.is_file() and f.suffix == ".rpm" and f.name not in wanted:
            try:
                f.unlink()
            except OSError as e:
                logger.warning("Could not remove stale local RPM %s: %s", f, e)

    for fname, (key, sz) in wanted.items():
        dest = rpms_dir / fname
        if sz > 0 and dest.is_file() and dest.stat().st_size == sz:
            continue
        data = client.get_object(key)
        if not data:
            raise RuntimeError(f"missing or empty RPM object at {key!r}, aborting repodata update")
        dest.write_bytes(data)

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


def _run_createrepo(repo_dir: Path) -> None:
    # Never use capture_output/PIPE here. createrepo_c can write a lot to stderr; a full pipe blocks the child
    # while the parent waits forever (looks like a hang with RPMs already on disk).
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
        logger.info("createrepo_c starting repo_root=%s workers=%s", repo_dir, workers)
        try:
            subprocess.run(
                [
                    "createrepo_c",
                    str(repo_dir),
                    f"--workers={workers}",
                ],
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
        logger.info("createrepo_c finished repo_root=%s workers=%s", repo_dir, workers)

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
            n = _sync_rpms_dir_from_r2(client, rpms_prefix, rpms_dir)
            _progress(f"{r2_prefix} synced {n} RPMs, running createrepo_c")
            with _global_repogen_lock():
                _run_createrepo(repo_root)
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
    Flow: download RPMs to temp dir, run createrepo_c, upload repodata + RPMs (or just repodata if RPMs already at prefix).
    """
    client = r2_client or get_r2_client()
    tmp_parent = _repo_temp_dir_parent()
    try:
        with tempfile.TemporaryDirectory(prefix="oreon-repo-", dir=tmp_parent) as tmp:
            repo_root = Path(tmp) / "repo"
            rpms_dir = repo_root / "RPMS"
            rpms_dir.mkdir(parents=True)
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
            _run_createrepo(repo_root)
            repodata_dir = repo_root / "repodata"
            if repodata_dir.exists():
                new_filenames = {f.name for f in repodata_dir.iterdir()}
                _upload_repodata_dir_ordered(client, r2_prefix, repodata_dir)

                _cleanup_old_repodata(client, r2_prefix, new_filenames)
            for key in rpm_keys:
                data = client.get_object(key)
                if not data:
                    raise RuntimeError(f"missing or empty RPM object at {key!r} during RPMS upload")
                fname = key.split("/")[-1]
                r2_key = f"{r2_prefix}/RPMS/{fname}"
                client.put_object(r2_key, data)
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
