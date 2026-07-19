from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

from oreon_build.services.publisher import update_repodata_at_prefix
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
    if staged_binaries and len(staged_binaries) <= 500:
        update_repodata_at_prefix(
            dest_prefix,
            r2_client=r2,
            staged_binaries=staged_binaries,
        )
    else:
        logger.info(
            "promotion dest repodata full regen prefix=%s staged=%s",
            dest_prefix,
            len(staged_binaries),
        )
        update_repodata_at_prefix(dest_prefix, r2_client=r2, force_full=True)

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
