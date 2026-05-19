"""Shared helpers for resolving build targets and skip-complete checks (used by builds router and chain builds)."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from oreon_build.models import BuildJob, BuildStatus, BuildTarget, MockEnvironment, Package, PackageVersion


async def get_or_create_build_target(
    db,
    release_id: int,
    architecture: str,
    mock_environment_id: int | None,
) -> BuildTarget:
    """Get or create a BuildTarget for (release_id, architecture, mock_environment_id)."""
    mock_env_id = mock_environment_id
    if mock_env_id is None:
        fb = await db.execute(
            select(MockEnvironment)
            .where(MockEnvironment.release_id == release_id)
            .order_by(MockEnvironment.id.asc())
            .limit(1)
        )
        me = fb.scalar_one_or_none()
        if me:
            mock_env_id = me.id

    q = (
        select(BuildTarget)
        .where(
            BuildTarget.release_id == release_id,
            BuildTarget.architecture == architecture,
            BuildTarget.mock_environment_id == mock_env_id,
        )
        .order_by(BuildTarget.id.asc())
        .limit(1)
    )
    result = await db.execute(q)
    target = result.scalars().first()
    if target:
        return target

    created = BuildTarget(
        release_id=release_id,
        architecture=architecture,
        mock_environment_id=mock_env_id,
    )
    db.add(created)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        db.expunge(created)
        again = await db.execute(q)
        found = again.scalars().first()
        if found:
            return found
        raise
    return created


async def latest_package_version_id(db, package_id: int) -> int | None:
    """Highest-id PackageVersion for a package, or None if the package has no versions."""
    return (
        await db.scalar(
            select(PackageVersion.id)
            .where(PackageVersion.package_id == package_id)
            .order_by(PackageVersion.id.desc())
            .limit(1)
        )
    )


async def has_successful_build_for_target(
    db,
    package_id: int,
    release_id: int,
    target_id: int,
    package_version_id: int | None,
    *,
    jobs_created_on_or_after: datetime | None = None,
) -> bool:
    """True if there is already a successful job for this package, release, and build target.

    When ``package_version_id`` is set, only successes for that version count.

    When ``jobs_created_on_or_after`` is set (used for chain builds), only successes
    from jobs created at or after that time count. That prevents ``skip_complete_arches``
    from treating pre-chain historical successes as satisfying a new chain after
    distgit/spec updates where jobs used unpinned ``package_version_id``.
    """
    q = (
        select(BuildJob.id)
        .where(
            BuildJob.package_id == package_id,
            BuildJob.release_id == release_id,
            BuildJob.target_id == target_id,
            BuildJob.status == BuildStatus.SUCCESS,
        )
        .limit(1)
    )
    if package_version_id is not None:
        q = q.where(BuildJob.package_version_id == package_version_id)
    if jobs_created_on_or_after is not None:
        ref = jobs_created_on_or_after
        if ref.tzinfo is None:
            from datetime import timezone as _tz

            ref = ref.replace(tzinfo=_tz.utc)
        q = q.where(BuildJob.created_at >= ref)
    r = await db.execute(q)
    return r.scalar_one_or_none() is not None


async def batch_package_ids_by_name(db, names: Iterable[str]) -> dict[str, int]:
    """Map package name -> id in one query."""
    uniq = sorted({(n or "").strip() for n in names if (n or "").strip()})
    if not uniq:
        return {}
    rows = await db.execute(select(Package.name, Package.id).where(Package.name.in_(uniq)))
    return {str(name): int(pid) for name, pid in rows.all()}


async def batch_latest_package_version_ids(db, package_ids: list[int]) -> dict[int, int | None]:
    """Highest PackageVersion id per package_id."""
    if not package_ids:
        return {}
    rows = await db.execute(
        select(
            PackageVersion.package_id,
            func.max(PackageVersion.id).label("vid"),
        )
        .where(PackageVersion.package_id.in_(package_ids))
        .group_by(PackageVersion.package_id)
    )
    out: dict[int, int | None] = {pid: None for pid in package_ids}
    for pkg_id, vid in rows.all():
        if vid is not None:
            out[int(pkg_id)] = int(vid)
    return out


async def package_ids_with_success_on_all_targets(
    db,
    *,
    package_ids: list[int],
    release_id: int,
    target_ids: list[int],
    version_by_package: dict[int, int | None],
    jobs_created_on_or_after: datetime | None,
) -> set[int]:
    """package_ids that already have SUCCESS on every target_id (with matching package_version_id)."""
    if not package_ids or not target_ids:
        return set()
    required_targets = set(target_ids)
    q = select(
        BuildJob.package_id,
        BuildJob.target_id,
        BuildJob.package_version_id,
    ).where(
        BuildJob.package_id.in_(package_ids),
        BuildJob.release_id == release_id,
        BuildJob.target_id.in_(target_ids),
        BuildJob.status == BuildStatus.SUCCESS,
    )
    if jobs_created_on_or_after is not None:
        ref = jobs_created_on_or_after
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        q = q.where(BuildJob.created_at >= ref)
    rows = await db.execute(q)
    hits: dict[int, set[int]] = defaultdict(set)
    for pid, tid, pvid in rows.all():
        pid_i = int(pid)
        expected_pv = version_by_package.get(pid_i)
        if expected_pv is not None and pvid != expected_pv:
            continue
        if expected_pv is None and pvid is not None:
            continue
        hits[pid_i].add(int(tid))
    return {pid for pid in package_ids if required_targets <= hits.get(pid, set())}
