from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oreon_build.models import BuildAttempt, BuildJob, PackageVersion, Source

async def source_r2_keys_for_attempt(db: AsyncSession, attempt_id: int) -> set[str]:
    out: set[str] = set()
    ar = await db.execute(select(BuildAttempt).where(BuildAttempt.id == attempt_id))
    attempt = ar.scalar_one_or_none()
    if not attempt:
        return out
    jr = await db.execute(select(BuildJob).where(BuildJob.id == attempt.build_job_id))
    job = jr.scalar_one_or_none()
    if not job:
        return out
    if job.package_version_id:
        src_result = await db.execute(
            select(Source).where(Source.package_version_id == job.package_version_id)
        )
    elif job.package_id:
        src_result = await db.execute(
            select(Source)
            .join(PackageVersion, Source.package_version_id == PackageVersion.id)
            .where(PackageVersion.package_id == job.package_id)
        )
    else:
        return out
    for src in src_result.scalars().all():
        k = (src.r2_key or "").strip()
        if k:
            out.add(k)
    return out
