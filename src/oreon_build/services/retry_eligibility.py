from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select

from oreon_build.models import BuildAttempt, BuildJob, Source


async def distgit_head_for_job(db: Any, job: BuildJob) -> str | None:
    if job.package_version_id is None:
        return None
    source = await db.scalar(
        select(Source)
        .where(
            Source.package_version_id == job.package_version_id,
            Source.kind == "distgit",
        )
        .limit(1)
    )
    if source is None or not source.url:
        return None
    base, marker, ref_path = source.url.partition("#")
    if not marker:
        return None
    ref, marker, _path = ref_path.partition(":")
    if not marker or not ref:
        return None
    parts = base.rstrip("/").removesuffix(".git").split("/")
    if len(parts) < 2:
        return None
    owner, repo = parts[-2:]
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}",
                headers={"Accept": "application/vnd.github+json"},
            )
        if response.status_code != 200:
            return None
        return str(response.json().get("sha") or "").strip() or None
    except httpx.HTTPError:
        return None


async def source_failure_can_retry(
    db: Any,
    job: BuildJob,
    attempt: BuildAttempt | None,
    *,
    force: bool = False,
) -> tuple[bool, str | None]:
    if attempt is None or attempt.failure_kind != "source_missing_local_file":
        return True, None
    if force:
        return True, "forced deterministic source retry"
    details = attempt.failure_details if isinstance(attempt.failure_details, dict) else {}
    failed_commit = str(details.get("commit") or "").strip()
    current_commit = await distgit_head_for_job(db, job)
    if current_commit and failed_commit and current_commit != failed_commit:
        return True, "retry after distgit revision changed"
    files = [str(item) for item in details.get("missing_local_files") or []]
    suffix = f": {', '.join(files)}" if files else ""
    revision = f" at {failed_commit}" if failed_commit else ""
    return False, f"required distgit source files were missing{suffix}{revision}"
