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

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import List

from oreon_build.services.r2 import R2Client, get_r2_client
from oreon_build.services.signing import sign_repomd

logger = logging.getLogger(__name__)


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


def update_repodata_at_prefix(r2_prefix: str, r2_client: R2Client | None = None) -> None:
    """
    RPMs are already at r2_prefix/RPMS/. Download them, run createrepo_c, upload only repodata.
    Use this when the worker (or other path) has already uploaded RPMs directly to the repo.
    """
    client = r2_client or get_r2_client()
    rpms_prefix = f"{r2_prefix}/RPMS/"
    objs = client.list_objects(rpms_prefix)
    rpm_keys = [o["key"] for o in objs if o.get("key")]
    if not rpm_keys:
        logger.warning("No RPMs at %s, skipping createrepo", rpms_prefix)
        return
    with tempfile.TemporaryDirectory(prefix="oreon-repo-") as tmp:
        repo_dir = Path(tmp) / "repo"
        repo_dir.mkdir()
        for key in rpm_keys:
            data = client.get_object(key)
            if data:
                fname = key.split("/")[-1]
                (repo_dir / fname).write_bytes(data)
        _run_createrepo(repo_dir)
        repodata_dir = repo_dir / "repodata"
        if repodata_dir.exists():
            # Keep track of which repodata filenames we just generated so we can
            # delete older R2 objects that createrepo no longer produces.
            new_filenames = {f.name for f in repodata_dir.iterdir()}

            repomd = repodata_dir / "repomd.xml"
            if repomd.exists():
                sign_repomd(repomd)

            # Upload new repodata first; only after that do we purge stale objects.
            for f in repodata_dir.iterdir():
                r2_key = f"{r2_prefix}/repodata/{f.name}"
                client.put_object(r2_key, f.read_bytes())

            _cleanup_old_repodata(client, r2_prefix, new_filenames)
    logger.info("Updated repodata at R2 prefix %s (%s RPMs)", r2_prefix, len(rpm_keys))


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
    with tempfile.TemporaryDirectory(prefix="oreon-repo-") as tmp:
        repo_dir = Path(tmp) / "repo"
        repo_dir.mkdir()
        for key in rpm_keys:
            data = client.get_object(key)
            if data:
                fname = key.split("/")[-1]
                (repo_dir / fname).write_bytes(data)
        _run_createrepo(repo_dir)
        repodata_dir = repo_dir / "repodata"
        if repodata_dir.exists():
            new_filenames = {f.name for f in repodata_dir.iterdir()}
            for f in repodata_dir.iterdir():
                r2_key = f"{r2_prefix}/repodata/{f.name}"
                client.put_object(r2_key, f.read_bytes())

            _cleanup_old_repodata(client, r2_prefix, new_filenames)
        for key in rpm_keys:
            data = client.get_object(key)
            if data:
                fname = key.split("/")[-1]
                r2_key = f"{r2_prefix}/RPMS/{fname}"
                client.put_object(r2_key, data)
    logger.info("Published repo to R2 prefix %s", r2_prefix)
