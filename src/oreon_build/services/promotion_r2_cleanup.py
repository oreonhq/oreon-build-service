from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from typing import Optional

from oreon_build.services.publisher import publish_rpms_to_r2, update_repodata_at_prefix
from oreon_build.services.r2 import R2Client, get_r2_client, repo_r2_prefix, repo_rpms_key, src_r2_key

logger = logging.getLogger(__name__)

_REPO_CHANNELS = ("dev", "testing", "stable")

def _rpm_name_from_bytes(data: bytes) -> Optional[str]:
    if not data:
        return None
    path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".rpm", delete=False) as f:
            f.write(data)
            path = f.name
        r = subprocess.run(
            ["rpm", "-qp", "--qf", "%{NAME}", path],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if r.returncode != 0:
            logger.warning("rpm -qp failed: %s", (r.stderr or r.stdout or "").strip())
            return None
        name = (r.stdout or "").strip()
        return name or None
    except FileNotFoundError:
        logger.warning("rpm binary not found; skipping RPM name-based R2 prune")
        return None
    except OSError as e:
        logger.warning("rpm name query failed: %s", e)
        return None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

def _collect_binary_rpm_names(
    r2: R2Client,
    releasename: str,
    from_branch: str,
    arch: str,
    rpm_filenames: list[str],
) -> set[str]:
    names: set[str] = set()
    for fn in rpm_filenames:
        if fn.endswith(".src.rpm"):
            continue
        key = repo_rpms_key(releasename, from_branch, arch, fn)
        data = r2.get_object(key)
        if not data:
            continue
        n = _rpm_name_from_bytes(data)
        if n:
            names.add(n)
    return names

def _collect_src_rpm_names(
    r2: R2Client,
    releasename: str,
    from_branch: str,
    rpm_filenames: list[str],
) -> set[str]:
    names: set[str] = set()
    for fn in rpm_filenames:
        if not fn.endswith(".src.rpm"):
            continue
        key = src_r2_key(releasename, from_branch, fn)
        data = r2.get_object(key)
        if not data:
            continue
        n = _rpm_name_from_bytes(data)
        if n:
            names.add(n)
    return names

def _prune_binary_rpms_matching_names(
    r2: R2Client,
    releasename: str,
    arch: str,
    keep_basenames: set[str],
    name_set: set[str],
) -> None:
    if not name_set:
        return
    for channel in _REPO_CHANNELS:
        prefix = f"{repo_r2_prefix(releasename, channel, arch)}/RPMS/"
        for obj in r2.list_objects(prefix):
            key = obj.get("key")
            if not key:
                continue
            base = key.rsplit("/", 1)[-1]
            if not base.endswith(".rpm") or base.endswith(".src.rpm"):
                continue
            if base in keep_basenames:
                continue
            if not any(base.startswith(n + "-") for n in name_set):
                continue
            data = r2.get_object(key)
            if not data:
                continue
            old_name = _rpm_name_from_bytes(data)
            if old_name and old_name in name_set:
                r2.delete_object(key)
                logger.info("Pruned superseded binary RPM from R2: %s", key)

def _prune_src_rpms_matching_names(
    r2: R2Client,
    releasename: str,
    keep_basenames: set[str],
    name_set: set[str],
) -> None:
    if not name_set:
        return
    for channel in _REPO_CHANNELS:
        prefix = f"{releasename}/{channel}/src/"
        for obj in r2.list_objects(prefix):
            key = obj.get("key")
            if not key:
                continue
            base = key.rsplit("/", 1)[-1]
            if not base.endswith(".src.rpm"):
                continue
            if base in keep_basenames:
                continue
            if not any(base.startswith(n + "-") for n in name_set):
                continue
            data = r2.get_object(key)
            if not data:
                continue
            old_name = _rpm_name_from_bytes(data)
            if old_name and old_name in name_set:
                r2.delete_object(key)
                logger.info("Pruned superseded SRPM from R2: %s", key)

def _remove_promoted_objects_from_branch(
    r2: R2Client,
    releasename: str,
    from_branch: str,
    arch: str,
    rpm_filenames: list[str],
) -> None:
    for fn in rpm_filenames:
        if fn.endswith(".src.rpm"):
            key = src_r2_key(releasename, from_branch, fn)
        else:
            key = repo_rpms_key(releasename, from_branch, arch, fn)
        try:
            r2.delete_object(key)
        except Exception as e:
            logger.warning("Could not delete promoted object %s: %s", key, e)

def run_post_promotion_r2_maintenance(
    releasename: str,
    from_branch: str,
    dest_channel: str,
    arch: str,
    artifact_filenames: list[str],
    copied_filenames: list[str],
) -> None:
    r2 = get_r2_client()
    keep_basenames = set(artifact_filenames)
    binary_names = _collect_binary_rpm_names(r2, releasename, from_branch, arch, copied_filenames)
    _prune_binary_rpms_matching_names(r2, releasename, arch, keep_basenames, binary_names)

    src_names = _collect_src_rpm_names(r2, releasename, from_branch, copied_filenames)
    _prune_src_rpms_matching_names(r2, releasename, keep_basenames, src_names)

    _remove_promoted_objects_from_branch(r2, releasename, from_branch, arch, copied_filenames)

    dest_prefix = repo_r2_prefix(releasename, dest_channel, arch)
    existing = r2.list_objects(f"{dest_prefix}/RPMS/")
    all_keys = [o["key"] for o in existing if o.get("key")]
    if all_keys:
        publish_rpms_to_r2(dest_prefix, all_keys, r2_client=r2)

    update_repodata_at_prefix(repo_r2_prefix(releasename, from_branch, arch), r2_client=r2)
