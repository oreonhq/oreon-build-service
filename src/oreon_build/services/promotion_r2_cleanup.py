# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

from oreon_build.services.publisher import prefix_publish_lock, rpm_publish_locks, update_repodata_at_prefix
from oreon_build.services.r2 import R2Client, get_r2_client, repo_r2_prefix, repo_rpms_key, src_r2_key

logger = logging.getLogger(__name__)

_REPO_CHANNELS = ("dev", "testing", "stable")
_COPY_WORKERS = 64


def rpm_name_from_filename(filename: str) -> Optional[str]:
    fn = (filename or "").strip()
    if not fn.endswith(".rpm"):
        return None
    stem = fn[:-4]
    if stem.endswith(".src"):
        stem = stem[:-4]
    else:
        dot = stem.rfind(".")
        if dot <= 0:
            return None
        stem = stem[:dot]
    parts = stem.rsplit("-", 2)
    if len(parts) != 3:
        return None
    name = parts[0].strip()
    return name or None


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
            timeout=60,
            check=False,
        )
        if r.returncode != 0:
            return None
        name = (r.stdout or "").strip()
        return name or None
    except (FileNotFoundError, OSError):
        return None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def list_branch_basenames(
    r2: R2Client,
    releasename: str,
    branch: str,
    arch: str,
) -> tuple[set[str], set[str]]:
    binary: set[str] = set()
    src: set[str] = set()
    bin_prefix = f"{repo_r2_prefix(releasename, branch, arch)}/RPMS/"
    for key in r2.iter_keys_with_prefix(bin_prefix):
        base = key.rsplit("/", 1)[-1]
        if base.endswith(".rpm") and not base.endswith(".src.rpm"):
            binary.add(base)
    src_prefix = f"{releasename}/{branch}/src/"
    for key in r2.iter_keys_with_prefix(src_prefix):
        base = key.rsplit("/", 1)[-1]
        if base.endswith(".src.rpm"):
            src.add(base)
    return binary, src


def _split_filenames(rpm_filenames: list[str]) -> tuple[list[str], list[str]]:
    binaries: list[str] = []
    srcs: list[str] = []
    for fn in rpm_filenames:
        if fn.endswith(".src.rpm"):
            srcs.append(fn)
        elif fn.endswith(".rpm"):
            binaries.append(fn)
    return binaries, srcs


@dataclass(frozen=True)
class PromoCopyPlan:
    promo_id: int
    filenames: list[str]
    kind: str


def classify_promotion_artifacts(
    *,
    promo_id: int,
    rpm_filenames: list[str],
    src_binaries: set[str],
    src_srcs: set[str],
    dest_binaries: set[str],
    dest_srcs: set[str],
) -> PromoCopyPlan:
    binaries, srcs = _split_filenames(rpm_filenames)
    if not binaries and not srcs:
        return PromoCopyPlan(promo_id=promo_id, filenames=[], kind="missing")

    if not binaries:
        if all(fn in src_srcs for fn in srcs):
            return PromoCopyPlan(promo_id=promo_id, filenames=list(srcs), kind="copy")
        if all(fn in dest_srcs for fn in srcs):
            return PromoCopyPlan(promo_id=promo_id, filenames=list(srcs), kind="already")
        return PromoCopyPlan(promo_id=promo_id, filenames=[], kind="missing")

    if all(fn in src_binaries for fn in binaries):
        available = list(binaries) + [fn for fn in srcs if fn in src_srcs]
        return PromoCopyPlan(promo_id=promo_id, filenames=available, kind="copy")
    if all(fn in dest_binaries for fn in binaries):
        available = list(binaries) + [fn for fn in srcs if fn in dest_srcs]
        return PromoCopyPlan(promo_id=promo_id, filenames=available, kind="already")
    return PromoCopyPlan(promo_id=promo_id, filenames=[], kind="missing")


def _copy_one(r2: R2Client, src_key: str, dest_key: str) -> tuple[str, str, bool]:
    ok = r2.copy_object(src_key, dest_key)
    return src_key, dest_key, ok


def copy_filenames_parallel(
    *,
    releasename: str,
    from_branch: str,
    to_branch: str,
    arch: str,
    rpm_filenames: list[str],
    r2: R2Client | None = None,
    workers: int = _COPY_WORKERS,
) -> list[str]:
    client = r2 or get_r2_client()
    pairs: list[tuple[str, str, str]] = []
    for fn in rpm_filenames:
        if fn.endswith(".src.rpm"):
            pairs.append((fn, src_r2_key(releasename, from_branch, fn), src_r2_key(releasename, to_branch, fn)))
        else:
            pairs.append(
                (
                    fn,
                    repo_rpms_key(releasename, from_branch, arch, fn),
                    repo_rpms_key(releasename, to_branch, arch, fn),
                )
            )
    if not pairs:
        return []
    copied: list[str] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pairs)))) as pool:
        futs = {pool.submit(_copy_one, client, src, dest): fn for fn, src, dest in pairs}
        for fut in as_completed(futs):
            fn = futs[fut]
            _src, _dest, ok = fut.result()
            if ok:
                copied.append(fn)
            else:
                failed.append(fn)
    if failed:
        raise RuntimeError(
            f"promotion copy failed for {len(failed)} object(s) after inventory check: "
            + ", ".join(failed[:8])
            + ("..." if len(failed) > 8 else "")
        )
    return copied


def list_release_arches(architectures_csv: str | None) -> list[str]:
    raw = (architectures_csv or "x86_64,aarch64").strip()
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        a = part.strip()
        if a and a not in out:
            out.append(a)
    return out or ["x86_64", "aarch64"]


def whole_tree_promote_branch(
    *,
    releasename: str,
    from_branch: str,
    to_branch: str,
    arches: list[str] | None = None,
    empty_source: bool = True,
    delete_source_prefix: bool = False,
    r2: R2Client | None = None,
) -> dict[str, int]:
    """Copy entire branch tree to dest, prune superseded on dest, empty/delete source."""
    client = r2 or get_r2_client()
    rel = (releasename or "").strip()
    src_b = (from_branch or "").strip()
    dest_b = (to_branch or "").strip()
    if not rel or not src_b or not dest_b:
        raise ValueError("releasename/from_branch/to_branch required")
    if src_b == dest_b:
        raise ValueError("from_branch and to_branch must differ")
    arch_list = list(arches or ["x86_64", "aarch64"])
    copied_total = 0
    for arch in arch_list:
        src_bin, src_src = list_branch_basenames(client, rel, src_b, arch)
        filenames = sorted(src_bin | src_src)
        if not filenames:
            continue
        src_prefix = repo_r2_prefix(rel, src_b, arch)
        dest_prefix = repo_r2_prefix(rel, dest_b, arch)
        lock_prefixes = sorted((src_prefix, dest_prefix))
        with prefix_publish_lock(lock_prefixes[0]), prefix_publish_lock(lock_prefixes[1]), rpm_publish_locks(dest_prefix, filenames):
            _dest_keys, copied = copy_branch_to_branch(
                rel, src_b, dest_b, arch, filenames, r2=client
            )
            copied_total += len(copied)
            run_batched_promotion_r2_maintenance(
                releasename=rel,
                from_branch=src_b,
                dest_channel=dest_b,
                arch=arch,
                artifact_filenames=filenames,
                copied_filenames=copied if empty_source else [],
                newly_copied_filenames=copied,
            )
            if not empty_source and copied:
                update_repodata_at_prefix(
                    repo_r2_prefix(rel, dest_b, arch),
                    r2_client=client,
                    force_full=True,
                )

    # any leftover keys under source prefix (repodata etc)
    if delete_source_prefix:
        prefix = f"{rel}/{src_b}/"
        keys = list(client.iter_keys_with_prefix(prefix))
        for i in range(0, len(keys), 900):
            chunk = keys[i : i + 900]
            try:
                client.delete_objects(chunk)
            except Exception:
                for k in chunk:
                    try:
                        client.delete_object(k)
                    except Exception:
                        pass
        return {"copied": copied_total, "source_keys_deleted": len(keys)}

    if empty_source:
        # wipe leftover repodata under source after RPM remove
        for arch in arch_list:
            prefix = f"{repo_r2_prefix(rel, src_b, arch)}/repodata/"
            keys = list(client.iter_keys_with_prefix(prefix))
            if keys:
                try:
                    client.delete_objects(keys)
                except Exception:
                    pass
            update_repodata_at_prefix(repo_r2_prefix(rel, src_b, arch), r2_client=client, force_full=True)
        src_prefix = f"{rel}/{src_b}/src/"
        # src RPMs already removed by maintenance; clear empty leftovers
        leftovers = [
            k
            for k in client.iter_keys_with_prefix(f"{rel}/{src_b}/")
            if "/repodata/" in k or k.endswith("/")
        ]
        if leftovers:
            try:
                client.delete_objects(leftovers)
            except Exception:
                pass

    return {"copied": copied_total, "source_keys_deleted": 0}


def copy_branch_to_branch(
    releasename: str,
    from_branch: str,
    to_branch: str,
    arch: str,
    rpm_filenames: list[str],
    *,
    r2: R2Client | None = None,
    source_keys: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    client = r2 or get_r2_client()
    src_bin, src_src = list_branch_basenames(client, releasename, from_branch, arch)
    dest_bin, dest_src = list_branch_basenames(client, releasename, to_branch, arch)
    plan = classify_promotion_artifacts(
        promo_id=0,
        rpm_filenames=rpm_filenames,
        src_binaries=src_bin,
        src_srcs=src_src,
        dest_binaries=dest_bin,
        dest_srcs=dest_src,
    )
    if plan.kind == "missing":
        return [], []
    if plan.kind == "already":
        dest_keys = []
        for fn in plan.filenames:
            if fn.endswith(".src.rpm"):
                dest_keys.append(src_r2_key(releasename, to_branch, fn))
            else:
                dest_keys.append(repo_rpms_key(releasename, to_branch, arch, fn))
        return dest_keys, list(plan.filenames)
    copied = copy_filenames_parallel(
        releasename=releasename,
        from_branch=from_branch,
        to_branch=to_branch,
        arch=arch,
        rpm_filenames=plan.filenames,
        r2=client,
    )
    dest_keys = []
    for fn in copied:
        if fn.endswith(".src.rpm"):
            dest_keys.append(src_r2_key(releasename, to_branch, fn))
        else:
            dest_keys.append(repo_rpms_key(releasename, to_branch, arch, fn))
    return dest_keys, copied


def _names_from_filenames(rpm_filenames: list[str]) -> tuple[set[str], set[str]]:
    binary_names: set[str] = set()
    src_names: set[str] = set()
    for fn in rpm_filenames:
        n = rpm_name_from_filename(fn)
        if not n:
            continue
        if fn.endswith(".src.rpm"):
            src_names.add(n)
        else:
            binary_names.add(n)
    return binary_names, src_names


def _prune_binary_rpms_matching_names(
    r2: R2Client,
    releasename: str,
    arch: str,
    keep_basenames: set[str],
    name_set: set[str],
    *,
    listed_by_channel: dict[str, set[str]] | None = None,
) -> None:
    if not name_set:
        return
    to_delete: list[str] = []
    for channel in _REPO_CHANNELS:
        if listed_by_channel and channel in listed_by_channel:
            bases = listed_by_channel[channel]
            for base in bases:
                if base in keep_basenames:
                    continue
                old_name = rpm_name_from_filename(base)
                if old_name and old_name in name_set:
                    to_delete.append(repo_rpms_key(releasename, channel, arch, base))
            continue
        prefix = f"{repo_r2_prefix(releasename, channel, arch)}/RPMS/"
        for key in r2.iter_keys_with_prefix(prefix):
            base = key.rsplit("/", 1)[-1]
            if not base.endswith(".rpm") or base.endswith(".src.rpm"):
                continue
            if base in keep_basenames:
                continue
            old_name = rpm_name_from_filename(base)
            if not old_name:
                continue
            if old_name in name_set:
                to_delete.append(key)
    if to_delete:
        r2.delete_objects(to_delete)
        logger.info("Pruned %s superseded binary RPMs", len(to_delete))


def _prune_src_rpms_matching_names(
    r2: R2Client,
    releasename: str,
    keep_basenames: set[str],
    name_set: set[str],
    *,
    listed_by_channel: dict[str, set[str]] | None = None,
) -> None:
    if not name_set:
        return
    to_delete: list[str] = []
    for channel in _REPO_CHANNELS:
        if listed_by_channel and channel in listed_by_channel:
            bases = listed_by_channel[channel]
            for base in bases:
                if base in keep_basenames:
                    continue
                old_name = rpm_name_from_filename(base)
                if old_name and old_name in name_set:
                    to_delete.append(src_r2_key(releasename, channel, base))
            continue
        prefix = f"{releasename}/{channel}/src/"
        for key in r2.iter_keys_with_prefix(prefix):
            base = key.rsplit("/", 1)[-1]
            if not base.endswith(".src.rpm"):
                continue
            if base in keep_basenames:
                continue
            old_name = rpm_name_from_filename(base)
            if not old_name:
                continue
            if old_name in name_set:
                to_delete.append(key)
    if to_delete:
        r2.delete_objects(to_delete)
        logger.info("Pruned %s superseded SRPMs", len(to_delete))


def _remove_promoted_objects_from_branch(
    r2: R2Client,
    releasename: str,
    from_branch: str,
    arch: str,
    rpm_filenames: list[str],
) -> None:
    keys: list[str] = []
    for fn in rpm_filenames:
        if fn.endswith(".src.rpm"):
            keys.append(src_r2_key(releasename, from_branch, fn))
        else:
            keys.append(repo_rpms_key(releasename, from_branch, arch, fn))
    try:
        r2.delete_objects(keys)
    except Exception as e:
        logger.warning("Could not batch-delete promoted objects: %s", e)
        for key in keys:
            try:
                r2.delete_object(key)
            except Exception as e2:
                logger.warning("Could not delete promoted object %s: %s", key, e2)


def run_post_promotion_r2_maintenance(
    releasename: str,
    from_branch: str,
    dest_channel: str,
    arch: str,
    artifact_filenames: list[str],
    copied_filenames: list[str],
) -> None:
    run_batched_promotion_r2_maintenance(
        releasename=releasename,
        from_branch=from_branch,
        dest_channel=dest_channel,
        arch=arch,
        artifact_filenames=artifact_filenames,
        copied_filenames=copied_filenames,
        newly_copied_filenames=copied_filenames,
    )


def run_batched_promotion_r2_maintenance(
    *,
    releasename: str,
    from_branch: str,
    dest_channel: str,
    arch: str,
    artifact_filenames: list[str],
    copied_filenames: list[str],
    binary_listings: dict[str, set[str]] | None = None,
    src_listings: dict[str, set[str]] | None = None,
    newly_copied_filenames: list[str] | None = None,
) -> None:
    r2 = get_r2_client()
    keep_basenames = set(artifact_filenames)
    names_from = newly_copied_filenames or copied_filenames or artifact_filenames
    binary_names, src_names = _names_from_filenames(names_from)
    _prune_binary_rpms_matching_names(
        r2,
        releasename,
        arch,
        keep_basenames,
        binary_names,
        listed_by_channel=binary_listings,
    )
    _prune_src_rpms_matching_names(
        r2,
        releasename,
        keep_basenames,
        src_names,
        listed_by_channel=src_listings,
    )

    dest_prefix = repo_r2_prefix(releasename, dest_channel, arch)
    staged_binaries = [
        fn
        for fn in (newly_copied_filenames or [])
        if fn.endswith(".rpm") and not fn.endswith(".src.rpm")
    ]
    # Prefer staged/--update createrepo for the copied set. A "full" regen walks
    # every RPM already on the dest mirror (can be tens of thousands) and is way
    # slower than updating metadata for the packages just promoted.
    if staged_binaries:
        logger.info(
            "promotion dest repodata staged update prefix=%s staged=%s",
            dest_prefix,
            len(staged_binaries),
        )
        update_repodata_at_prefix(
            dest_prefix,
            r2_client=r2,
            staged_binaries=staged_binaries,
        )
    else:
        update_repodata_at_prefix(dest_prefix, r2_client=r2)

    if copied_filenames:
        _remove_promoted_objects_from_branch(
            r2, releasename, from_branch, arch, copied_filenames
        )
    update_repodata_at_prefix(
        repo_r2_prefix(releasename, from_branch, arch),
        r2_client=r2,
    )


@dataclass
class BulkGroupResult:
    promoted_ids: list[int]
    missing_ids: list[int]
    copied_count: int


def promote_group_from_inventory(
    *,
    releasename: str,
    from_branch: str,
    dest_channel: str,
    arch: str,
    items: list[tuple[int, list[str]]],
) -> BulkGroupResult:
    r2 = get_r2_client()
    logger.info(
        "bulk promote inventory start releasename=%s arch=%s from=%s dest=%s promos=%s",
        releasename,
        arch,
        from_branch,
        dest_channel,
        len(items),
    )
    src_bin, src_src = list_branch_basenames(r2, releasename, from_branch, arch)
    dest_bin, dest_src = list_branch_basenames(r2, releasename, dest_channel, arch)
    binary_listings = {from_branch: set(src_bin), dest_channel: set(dest_bin)}
    src_listings = {from_branch: set(src_src), dest_channel: set(dest_src)}
    for ch in _REPO_CHANNELS:
        if ch not in binary_listings:
            b, s = list_branch_basenames(r2, releasename, ch, arch)
            binary_listings[ch] = b
            src_listings[ch] = s

    to_copy: list[tuple[int, list[str]]] = []
    already: list[int] = []
    missing: list[int] = []
    by_id = {pid: filenames for pid, filenames in items}
    for promo_id, filenames in items:
        plan = classify_promotion_artifacts(
            promo_id=promo_id,
            rpm_filenames=filenames,
            src_binaries=src_bin,
            src_srcs=src_src,
            dest_binaries=dest_bin,
            dest_srcs=dest_src,
        )
        if plan.kind == "copy":
            to_copy.append((promo_id, plan.filenames))
        elif plan.kind == "already":
            already.append(promo_id)
        else:
            missing.append(promo_id)

    logger.info(
        "bulk promote inventory done releasename=%s arch=%s copy=%s already=%s missing=%s src_bin=%s dest_bin=%s",
        releasename,
        arch,
        len(to_copy),
        len(already),
        len(missing),
        len(src_bin),
        len(dest_bin),
    )

    promoted_ids = [pid for pid, _ in to_copy] + already
    if to_copy:
        uniq_copy = list(dict.fromkeys(fn for _pid, fns in to_copy for fn in fns))
        logger.info(
            "bulk promote copying %s objects (%s promos) workers=%s",
            len(uniq_copy),
            len(to_copy),
            _COPY_WORKERS,
        )
        copy_filenames_parallel(
            releasename=releasename,
            from_branch=from_branch,
            to_branch=dest_channel,
            arch=arch,
            rpm_filenames=uniq_copy,
            r2=r2,
        )
        dest_bin.update(fn for fn in uniq_copy if not fn.endswith(".src.rpm"))
        dest_src.update(fn for fn in uniq_copy if fn.endswith(".src.rpm"))
        binary_listings[dest_channel] = set(dest_bin)
        src_listings[dest_channel] = set(dest_src)

    if promoted_ids:
        keep = list(dict.fromkeys(fn for pid in promoted_ids for fn in by_id.get(pid, [])))
        newly_copied = list(dict.fromkeys(fn for _pid, fns in to_copy for fn in fns))
        remove_from_src = [
            fn
            for fn in newly_copied
            if (fn.endswith(".src.rpm") and fn in src_src)
            or ((not fn.endswith(".src.rpm")) and fn in src_bin)
        ]
        if not newly_copied:
            remove_from_src = [
                fn
                for fn in keep
                if (fn.endswith(".src.rpm") and fn in src_src)
                or ((not fn.endswith(".src.rpm")) and fn in src_bin)
            ]
        run_batched_promotion_r2_maintenance(
            releasename=releasename,
            from_branch=from_branch,
            dest_channel=dest_channel,
            arch=arch,
            artifact_filenames=keep,
            copied_filenames=remove_from_src,
            binary_listings=binary_listings,
            src_listings=src_listings,
            newly_copied_filenames=newly_copied,
        )

    return BulkGroupResult(
        promoted_ids=promoted_ids,
        missing_ids=missing,
        copied_count=sum(len(fns) for _pid, fns in to_copy),
    )
