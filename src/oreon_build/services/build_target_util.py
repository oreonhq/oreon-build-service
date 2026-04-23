"""Shared helpers for resolving build targets and skip-complete checks (used by builds router and chain builds)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oreon_build.models import BuildJob, BuildStatus, BuildTarget, MockEnvironment


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


async def has_successful_build_for_target(
    db,
    package_id: int,
    release_id: int,
    target_id: int,
    package_version_id: int | None,
) -> bool:
    """True if there is already a successful job for this package, release, and build target."""
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
    r = await db.execute(q)
    return r.scalar_one_or_none() is not None
