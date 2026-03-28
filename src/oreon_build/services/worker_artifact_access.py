# Oreon Build Service
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

"""Which R2 object keys a build attempt may read (worker proxy downloads)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oreon_build.models import BuildAttempt, BuildJob, PackageVersion, Source


async def source_r2_keys_for_attempt(db: AsyncSession, attempt_id: int) -> set[str]:
    """R2 keys listed on Source rows for this attempt job (same scope as poll ``sources``)."""
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
