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
    uniq = sorted({(n or "").strip() for n in names if (n or "").strip()})
    if not uniq:
        return {}
    rows = await db.execute(select(Package.name, Package.id).where(Package.name.in_(uniq)))
    return {str(name): int(pid) for name, pid in rows.all()}


async def batch_latest_package_version_ids(db, package_ids: list[int]) -> dict[int, int | None]:
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
