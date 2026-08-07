from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from oreon_build.models import BuildAttempt, BuildJob, BuildStatus, BuildTarget, Package, Release


def is_spec_not_found_error(msg: str | None) -> bool:
    if not msg:
        return False
    return "spec not found" in msg.lower()


def latest_job_slot_subquery():
    return (
        select(
            BuildJob.id.label("job_id"),
            func.row_number()
            .over(
                partition_by=(
                    BuildJob.package_id,
                    BuildJob.release_id,
                    func.coalesce(BuildTarget.architecture, ""),
                ),
                order_by=BuildJob.id.desc(),
            )
            .label("rn"),
        )
        .select_from(BuildJob)
        .outerjoin(BuildTarget, BuildJob.target_id == BuildTarget.id)
        .subquery()
    )


@dataclass
class CurrentFailureRow:
    job: BuildJob
    package_name: str
    release_name: str
    architecture: str | None
    latest_attempt: BuildAttempt | None


async def fetch_current_failure_rows(
    db: Any,
    *,
    layer_plan_only: bool = False,
) -> list[CurrentFailureRow]:
    ranked = latest_job_slot_subquery()
    stmt = (
        select(BuildJob, Package.name, Release.releasename, BuildTarget.architecture)
        .join(ranked, BuildJob.id == ranked.c.job_id)
        .join(Package, Package.id == BuildJob.package_id)
        .join(Release, Release.id == BuildJob.release_id)
        .outerjoin(BuildTarget, BuildJob.target_id == BuildTarget.id)
        .where(ranked.c.rn == 1)
        .where(BuildJob.status == BuildStatus.FAILED)
    )
    if layer_plan_only:
        stmt = stmt.where(BuildJob.layer_plan_id.isnot(None))
    stmt = stmt.order_by(BuildJob.completed_at.desc().nullslast(), BuildJob.id.desc())
    result = await db.execute(stmt)
    rows = result.all()
    if not rows:
        return []

    jobs = [r[0] for r in rows]
    job_ids = [j.id for j in jobs]
    att_rows = await db.execute(
        select(BuildAttempt)
        .where(BuildAttempt.build_job_id.in_(job_ids))
        .order_by(BuildAttempt.build_job_id.asc(), BuildAttempt.id.desc())
    )
    latest_by_job: dict[int, BuildAttempt] = {}
    for att in att_rows.scalars().all():
        if att.build_job_id not in latest_by_job:
            latest_by_job[att.build_job_id] = att

    out: list[CurrentFailureRow] = []
    for job, pkg_name, rel_name, arch in rows:
        out.append(
            CurrentFailureRow(
                job=job,
                package_name=pkg_name or "",
                release_name=rel_name or "",
                architecture=arch,
                latest_attempt=latest_by_job.get(job.id),
            )
        )
    return out
