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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""One-shot repair: build attempts marked failed but with RPM artifacts (HTTP timeout race)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker, selectinload

from oreon_build.config import get_settings
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import BuildAttempt, BuildJob, BuildStatus, Package, Promotion, Release


def repair_misclassified_builds(*, dry_run: bool) -> List[str]:
    """
    For each build job, if the **latest** attempt is FAILED but has at least one artifact, set
    attempt + job to SUCCESS and ensure a pending Promotion exists (matches successful build flow).

    Returns human-readable log lines.
    """
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    lines: list[str] = []

    with SessionLocal() as session:
        jobs = session.scalars(
            select(BuildJob).options(
                selectinload(BuildJob.attempts).selectinload(BuildAttempt.artifacts)
            )
        ).all()

        for job in jobs:
            attempts = sorted(job.attempts, key=lambda a: (a.attempt_number, a.id))
            if not attempts:
                continue
            latest = attempts[-1]
            if latest.status != BuildStatus.FAILED:
                continue
            if not latest.artifacts:
                continue

            lines.append(
                f"job={job.id} attempt={latest.id} (#{latest.attempt_number}): "
                f"failed -> success ({len(latest.artifacts)} artifacts)"
                + (" [dry-run]" if dry_run else "")
            )
            if dry_run:
                continue

            latest.status = BuildStatus.SUCCESS
            latest.error_message = None
            now = datetime.now(timezone.utc)
            latest.finished_at = latest.finished_at or now
            job.status = BuildStatus.SUCCESS
            job.completed_at = latest.finished_at
            rel = session.get(Release, job.release_id)
            if rel:
                job.branch = r2_publish_branch(rel)

            promo_n = session.scalar(
                select(func.count(Promotion.id)).where(Promotion.build_job_id == job.id)
            )
            if (promo_n or 0) == 0:
                pkg = session.get(Package, job.package_id)
                session.add(
                    Promotion(
                        release_id=job.release_id,
                        from_channel="dev",
                        to_channel=None,
                        status="pending",
                        package_id=job.package_id,
                        package_name=pkg.name if pkg else None,
                        build_job_id=job.id,
                    )
                )

        if not dry_run:
            session.commit()

    if not lines:
        lines.append("No misclassified builds found (latest attempt failed with artifacts).")
    return lines
