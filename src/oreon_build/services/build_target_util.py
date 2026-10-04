from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from oreon_build.models import (
    BuildJob,
    BuildStatus,
    BuildTarget,
    MockEnvironment,
    Package,
    PackageVersion,
    Release,
)


def is_bootstrap_mock_environment(
    *,
    name: str | None = None,
    config_name: str | None = None,
) -> bool:
    """Fedora/EL bootstrap chroots used to seed Oreon, not the release target mock."""
    n = (name or "").strip().lower()
    c = (config_name or "").strip().lower()
    if "bootstrap" in n:
        return True
    if c.startswith("fedora-") or c.startswith("centos-") or c.startswith("epel-"):
        return True
    return False


def resolve_mock_environment_id(
    *,
    trigger_mock_environment_id: int | None = None,
    package_mock_environment_id: int | None = None,
    release_default_mock_environment_id: int | None = None,
    ignore_package_mock: bool = False,
) -> int | None:
    if trigger_mock_environment_id is not None:
        return int(trigger_mock_environment_id)
    if not ignore_package_mock and package_mock_environment_id is not None:
        return int(package_mock_environment_id)
    if release_default_mock_environment_id is not None:
        return int(release_default_mock_environment_id)
    return None


def choose_mock_environment_id(
    *,
    preferred_id: int | None,
    release_id: int,
    release_default_id: int | None,
    environments: Iterable[Any],
) -> int | None:
    by_id: dict[int, Any] = {}
    for me in environments:
        if int(getattr(me, "release_id")) != int(release_id):
            continue
        by_id[int(me.id)] = me
    if preferred_id is not None and int(preferred_id) in by_id:
        return int(preferred_id)
    if release_default_id is not None and int(release_default_id) in by_id:
        return int(release_default_id)
    native = [
        me
        for me in by_id.values()
        if not is_bootstrap_mock_environment(
            name=getattr(me, "name", None),
            config_name=getattr(me, "config_name", None),
        )
    ]
    if native:
        native.sort(key=lambda m: int(m.id))
        return int(native[0].id)
    if by_id:
        return int(min(by_id))
    return None


async def pick_mock_environment_id_for_release(
    db,
    release_id: int,
    preferred_id: int | None = None,
) -> int | None:
    rel = await db.get(Release, release_id)
    rows = list(
        (
            await db.execute(
                select(MockEnvironment).where(MockEnvironment.release_id == release_id)
            )
        ).scalars().all()
    )
    return choose_mock_environment_id(
        preferred_id=preferred_id,
        release_id=release_id,
        release_default_id=rel.default_mock_environment_id if rel is not None else None,
        environments=rows,
    )


async def resolve_package_by_name(
    db,
    name: str,
    *,
    release_id: int | None = None,
) -> Package | None:
    pkg_name = (name or "").strip()
    if not pkg_name:
        return None
    result = await db.execute(
        select(Package).where(Package.name == pkg_name).order_by(Package.id.asc())
    )
    rows = list(result.scalars().all())
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]

    prefer_ids: list[int] = []
    if release_id is not None:
        rel_r = await db.execute(
            select(BuildJob.package_id)
            .join(Package, Package.id == BuildJob.package_id)
            .where(Package.name == pkg_name, BuildJob.release_id == release_id)
            .order_by(BuildJob.id.desc())
        )
        prefer_ids.extend(int(x) for x in rel_r.scalars().all() if x is not None)

    by_id = {p.id: p for p in rows}
    seen: set[int] = set()
    for pid in prefer_ids:
        if pid in seen:
            continue
        seen.add(pid)
        hit = by_id.get(pid)
        if hit is not None:
            return hit
    return rows[0]


async def get_or_create_build_target(
    db,
    release_id: int,
    architecture: str,
    mock_environment_id: int | None,
) -> BuildTarget:
    mock_env_id = mock_environment_id
    if mock_env_id is None:
        mock_env_id = await pick_mock_environment_id_for_release(db, release_id)

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
    ignore_bootstrap: bool = False,
) -> bool:
    q = (
        select(BuildJob)
        .where(
            BuildJob.package_id == package_id,
            BuildJob.release_id == release_id,
            BuildJob.target_id == target_id,
            BuildJob.status == BuildStatus.SUCCESS,
        )
    )
    if package_version_id is not None:
        q = q.where(BuildJob.package_version_id == package_version_id)
    if jobs_created_on_or_after is not None:
        ref = jobs_created_on_or_after
        if ref.tzinfo is None:
            from datetime import timezone as _tz

            ref = ref.replace(tzinfo=_tz.utc)
        q = q.where(BuildJob.created_at >= ref)
    rows = list((await db.execute(q)).scalars().all())
    if not ignore_bootstrap:
        return bool(rows)
    for job in rows:
        opts = job.job_options if isinstance(job.job_options, dict) else {}
        if str(opts.get("build_profile") or "full").strip().lower() != "bootstrap":
            return True
    return False


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
