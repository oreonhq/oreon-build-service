"""
Repository publishing: createrepo_c and upload to R2.
Repositories are written ONLY to R2, never stored locally.
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
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import List

from oreon_build.config import get_settings
from oreon_build.services.r2 import R2Client, get_r2_client
from oreon_build.services.signing import sign_repomd

logger = logging.getLogger(__name__)


def _repo_temp_dir_parent() -> str | None:
    """Use OREON_REPO_TMPDIR / settings.oreon_repo_tmpdir when set and usable (avoids /tmp quota)."""
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
        p = Path(c)
        if p.is_dir():
            return str(p)
    return None


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
    subprocess.run(
        ["createrepo_c", str(repo_dir)],
        check=True,
        capture_output=True,
    )


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
    _edquot = getattr(errno, "EDQUOT", 122)
    client = r2_client or get_r2_client()
    rpms_prefix = f"{r2_prefix}/RPMS/"
    objs = client.list_objects(rpms_prefix)
    rpm_keys = [o["key"] for o in objs if o.get("key")]
    if not rpm_keys:
        logger.warning("No RPMs at %s, skipping createrepo", rpms_prefix)
        return False
    tmp_parent = _repo_temp_dir_parent()
    try:
        with tempfile.TemporaryDirectory(prefix="oreon-repo-", dir=tmp_parent) as tmp:
            repo_root = Path(tmp) / "repo"
            rpms_dir = repo_root / "RPMS"
            rpms_dir.mkdir(parents=True)
            for key in rpm_keys:
                data = client.get_object(key)
                if not data:
                    raise RuntimeError(f"missing or empty RPM object at {key!r}, aborting repodata update")
                fname = key.split("/")[-1]
                dest = rpms_dir / fname
                if dest.exists():
                    logger.warning(
                        "Duplicate RPM basename in R2 listing, overwriting %s (key %s)",
                        fname,
                        key,
                    )
                dest.write_bytes(data)
            _run_createrepo(repo_root)
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
                logger.info("Updated repodata at R2 prefix %s (%s RPMs)", r2_prefix, len(rpm_keys))
                return True
            logger.error("createrepo_c produced no repodata under %s", repo_root)
            return False
    except OSError as e:
        if getattr(e, "errno", None) == _edquot:
            parent = tmp_parent or os.environ.get("TMPDIR", "") or "/tmp"
            logger.error(
                "Disk quota exceeded while composing repo metadata under %s. "
                "Set OREON_REPO_TMPDIR or oreon_repo_tmpdir to a directory on a larger volume.",
                parent,
            )
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
    logger.info("Published repo to R2 prefix %s", r2_prefix)
