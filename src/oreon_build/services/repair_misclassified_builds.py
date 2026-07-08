from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker, selectinload

from oreon_build.config import get_settings
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import BuildAttempt, BuildJob, BuildStatus, Package, Promotion, Release

def repair_misclassified_builds(*, dry_run: bool) -> List[str]:
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
