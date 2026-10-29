# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from fastapi import HTTPException, status
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import selectinload

from oreon_build.models import (
    Account,
    Artifact,
    BuildAttempt,
    BuildJob,
    BuildStatus,
    LayerPlan,
    LayerPlanStatus,
    Package,
    PublishTask,
    RpmbuildPlan,
    RpmbuildPlanStatus,
    SideTag,
    SideTagCreatedFrom,
    SideTagMember,
)
from oreon_build.services.r2 import get_r2_client

logger = logging.getLogger(__name__)

PRIMARY_REPO_BRANCHES = frozenset({"dev", "testing", "stable"})
RESERVED_SIDE_TAG_NAMES = frozenset(
    {"dev", "testing", "stable", "builds", "logs", "src", "repodata"}
)
_RE_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,126}[A-Za-z0-9]$|^[A-Za-z0-9]$")
INACTIVE_DAYS = 60


def sanitize_side_tag_name(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("refs/heads/"):
        s = s[len("refs/heads/") :]
    s = s.replace(" ", "-")
    s = re.sub(r"[^A-Za-z0-9._/-]+", "-", s)
    s = re.sub(r"/+", "/", s).strip("/.-")
    return s[:128]


def validate_side_tag_name(name: str) -> str:
    n = sanitize_side_tag_name(name)
    if not n:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Side tag name is empty")
    if n.lower() in RESERVED_SIDE_TAG_NAMES or n.lower() in PRIMARY_REPO_BRANCHES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Side tag name '{n}' is reserved for primary or internal branches",
        )
    if not _RE_SAFE_NAME.match(n):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Side tag name must be a safe branch-like segment",
        )
    return n


def is_primary_repo_branch(name: str | None) -> bool:
    return (name or "").strip().lower() in PRIMARY_REPO_BRANCHES


def can_access_side_tag(user: Account | None, tag: SideTag, member_ids: Iterable[int] | None = None) -> bool:
    if user is None:
        return False
    if user.is_admin:
        return True
    if tag.owner_account_id is not None and int(tag.owner_account_id) == int(user.id):
        return True
    ids = set(int(x) for x in (member_ids if member_ids is not None else ()))
    if member_ids is None:
        ids = {int(m.account_id) for m in (tag.members or [])}
    return int(user.id) in ids


def require_side_tag_access(user: Account, tag: SideTag, member_ids: Iterable[int] | None = None) -> None:
    if not can_access_side_tag(user, tag, member_ids=member_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not allowed to use this side tag",
        )


async def bump_side_tag_activity(db: Any, side_tag_id: int | None) -> None:
    if not side_tag_id:
        return
    now = datetime.now(timezone.utc)
    await db.execute(
        update(SideTag).where(SideTag.id == int(side_tag_id)).values(last_activity_at=now)
    )


async def get_side_tag(db: Any, side_tag_id: int, *, with_members: bool = True) -> SideTag | None:
    q = select(SideTag).where(SideTag.id == int(side_tag_id))
    if with_members:
        q = q.options(
            selectinload(SideTag.members).selectinload(SideTagMember.account),
            selectinload(SideTag.owner),
            selectinload(SideTag.release),
        )
    return (await db.execute(q)).scalar_one_or_none()


async def get_side_tag_by_name(db: Any, release_id: int, name: str) -> SideTag | None:
    n = sanitize_side_tag_name(name)
    if not n:
        return None
    q = (
        select(SideTag)
        .where(SideTag.release_id == int(release_id), SideTag.name == n)
        .options(
            selectinload(SideTag.members).selectinload(SideTagMember.account),
            selectinload(SideTag.owner),
        )
    )
    return (await db.execute(q)).scalar_one_or_none()


async def create_side_tag(
    db: Any,
    *,
    release_id: int,
    name: str,
    owner_account_id: int | None,
    created_from: SideTagCreatedFrom = SideTagCreatedFrom.MANUAL,
) -> SideTag:
    n = validate_side_tag_name(name)
    existing = await get_side_tag_by_name(db, release_id, n)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Side tag '{n}' already exists for this release",
        )
    tag = SideTag(
        release_id=int(release_id),
        name=n,
        owner_account_id=owner_account_id,
        created_from=created_from,
        last_activity_at=datetime.now(timezone.utc),
    )
    db.add(tag)
    await db.flush()
    return tag


async def ensure_side_tag(
    db: Any,
    *,
    release_id: int,
    name: str,
    owner_account_id: int | None,
    created_from: SideTagCreatedFrom,
) -> SideTag:
    n = validate_side_tag_name(name)
    existing = await get_side_tag_by_name(db, release_id, n)
    if existing:
        await bump_side_tag_activity(db, existing.id)
        return existing
    return await create_side_tag(
        db,
        release_id=release_id,
        name=n,
        owner_account_id=owner_account_id,
        created_from=created_from,
    )


async def resolve_required_side_tag(
    db: Any,
    *,
    release_id: int,
    side_tag_id: int | None,
    user: Account | None = None,
    require_access: bool = True,
) -> SideTag:
    if side_tag_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="side_tag_id is required; builds only publish into side tags",
        )
    tag = await get_side_tag(db, int(side_tag_id))
    if tag is None or int(tag.release_id) != int(release_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Side tag not found")
    if require_access and user is not None:
        require_side_tag_access(user, tag)
    return tag


async def side_tag_is_busy(db: Any, side_tag_id: int) -> bool:
    open_job = await db.execute(
        select(BuildJob.id)
        .where(
            BuildJob.side_tag_id == int(side_tag_id),
            BuildJob.status.in_(
                [BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING]
            ),
        )
        .limit(1)
    )
    if open_job.scalar_one_or_none() is not None:
        return True
    open_chain = await db.execute(
        select(LayerPlan.id)
        .where(
            LayerPlan.side_tag_id == int(side_tag_id),
            LayerPlan.status.in_([LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING]),
        )
        .limit(1)
    )
    if open_chain.scalar_one_or_none() is not None:
        return True
    tag = await db.get(SideTag, int(side_tag_id))
    if tag is None:
        return False
    open_plan = await db.execute(
        select(RpmbuildPlan.id)
        .where(
            RpmbuildPlan.release_id == tag.release_id,
            RpmbuildPlan.repo_branch == tag.name,
            RpmbuildPlan.status.in_(
                [
                    RpmbuildPlanStatus.PENDING,
                    RpmbuildPlanStatus.RUNNING,
                    RpmbuildPlanStatus.WAITING_SETTLE,
                ]
            ),
        )
        .limit(1)
    )
    return open_plan.scalar_one_or_none() is not None


def delete_side_tag_r2_prefix(releasename: str, tag_name: str) -> int:
    rel = (releasename or "").strip()
    name = (tag_name or "").strip()
    if not rel or not name or is_primary_repo_branch(name):
        return 0
    r2 = get_r2_client()
    prefix = f"{rel}/{name}/"
    keys = list(r2.iter_keys_with_prefix(prefix))
    if not keys:
        return 0
    # batch delete
    for i in range(0, len(keys), 900):
        chunk = keys[i : i + 900]
        try:
            r2.delete_objects(chunk)
        except Exception:
            logger.exception("side tag r2 delete batch failed prefix=%s", prefix)
            for k in chunk:
                try:
                    r2.delete_object(k)
                except Exception:
                    pass
    return len(keys)


async def cancel_open_work_for_side_tag(db: Any, side_tag_id: int) -> None:
    now = datetime.now(timezone.utc)
    await db.execute(
        update(BuildJob)
        .where(
            BuildJob.side_tag_id == int(side_tag_id),
            BuildJob.status.in_(
                [BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING]
            ),
        )
        .values(status=BuildStatus.CANCELLED, completed_at=now)
    )
    await db.execute(
        update(LayerPlan)
        .where(
            LayerPlan.side_tag_id == int(side_tag_id),
            LayerPlan.status.in_([LayerPlanStatus.PENDING, LayerPlanStatus.RUNNING]),
        )
        .values(
            status=LayerPlanStatus.CANCELLED,
            error_message="Side tag scrapped",
        )
    )


async def scrap_side_tag(db: Any, tag: SideTag, *, releasename: str) -> dict[str, Any]:
    await cancel_open_work_for_side_tag(db, tag.id)
    tag_id = tag.id
    name = tag.name
    await db.execute(delete(SideTagMember).where(SideTagMember.side_tag_id == tag_id))
    await db.execute(
        update(BuildJob).where(BuildJob.side_tag_id == tag_id).values(side_tag_id=None)
    )
    await db.execute(
        update(LayerPlan).where(LayerPlan.side_tag_id == tag_id).values(side_tag_id=None)
    )
    from oreon_build.models import ReleaseMonitorScan

    await db.execute(
        update(ReleaseMonitorScan).where(ReleaseMonitorScan.side_tag_id == tag_id).values(side_tag_id=None)
    )
    await db.execute(delete(SideTag).where(SideTag.id == tag_id))
    await db.flush()

    async def _wipe() -> None:
        try:
            n = await asyncio.to_thread(delete_side_tag_r2_prefix, releasename, name)
            logger.info("scrapped side tag %s deleted %s objects", name, n)
        except Exception:
            logger.exception("scrap side tag r2 wipe failed tag=%s", name)

    asyncio.create_task(_wipe())
    return {"id": tag_id, "name": name, "releasename": releasename}


def _side_tag_prefix(releasename: str, tag_name: str) -> str:
    return f"{(releasename or '').strip()}/{(tag_name or '').strip()}/"


async def delete_side_tag_packages(
    db: Any,
    tag: SideTag,
    names: list[str],
    *,
    releasename: str,
) -> dict[str, Any]:
    wanted = []
    seen = set()
    for raw in names:
        n = (raw or "").strip()
        if not n or n in seen:
            continue
        seen.add(n)
        wanted.append(n)
    if not wanted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No package names")
    rows = (
        await db.execute(select(Package).where(Package.name.in_(wanted)))
    ).scalars().all()
    by_name = {p.name: p for p in rows}
    missing = [n for n in wanted if n not in by_name]
    if missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown package " + ", ".join(missing))
    pkg_ids = [p.id for p in rows]
    jobs = (
        await db.execute(
            select(BuildJob).where(
                BuildJob.side_tag_id == tag.id,
                BuildJob.package_id.in_(pkg_ids),
            )
        )
    ).scalars().all()
    if not jobs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="None of those packages are on this side tag")
    job_ids = [j.id for j in jobs]
    attempt_ids = [
        r[0]
        for r in (
            await db.execute(select(BuildAttempt.id).where(BuildAttempt.build_job_id.in_(job_ids)))
        ).all()
    ]
    filenames: set[str] = set()
    keys: set[str] = set()
    prefix = _side_tag_prefix(releasename, tag.name)
    if attempt_ids:
        arts = (
            await db.execute(
                select(Artifact.filename, Artifact.r2_key).where(Artifact.build_attempt_id.in_(attempt_ids))
            )
        ).all()
        for filename, r2_key in arts:
            if filename and str(filename).endswith(".rpm"):
                filenames.add(str(filename))
            if r2_key and str(r2_key).startswith(prefix):
                keys.add(str(r2_key))
    if prefix.strip("/"):
        r2 = get_r2_client()
        if filenames:
            for key in await asyncio.to_thread(lambda: list(r2.iter_keys_with_prefix(prefix))):
                if "/repodata/" in key:
                    continue
                base = key.rsplit("/", 1)[-1]
                if base in filenames:
                    keys.add(key)
        if keys:
            await asyncio.to_thread(r2.delete_objects, sorted(keys))
    now = datetime.now(timezone.utc)
    await db.execute(
        update(BuildJob)
        .where(
            BuildJob.id.in_(job_ids),
            BuildJob.status.in_(
                [
                    BuildStatus.PENDING,
                    BuildStatus.QUEUED,
                    BuildStatus.RUNNING,
                    BuildStatus.BLOCKED,
                    BuildStatus.SUCCESS,
                ]
            ),
        )
        .values(status=BuildStatus.CANCELLED, completed_at=now, blocked_reason="Removed from side tag", scan_state="skipped")
    )
    await db.execute(
        update(BuildAttempt)
        .where(
            BuildAttempt.build_job_id.in_(job_ids),
            BuildAttempt.status.in_(
                (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)
            ),
        )
        .values(status=BuildStatus.CANCELLED, finished_at=now, error_message="Removed from side tag")
    )
    await db.execute(
        update(PublishTask)
        .where(
            PublishTask.build_job_id.in_(job_ids),
            PublishTask.status.in_(("pending", "queued", "running")),
        )
        .values(status="cancelled", error_message="package removed from side tag", finished_at=now)
    )
    await bump_side_tag_activity(db, tag.id)
    arches = []
    rel = tag.release
    if rel is not None and hasattr(rel, "arch_list"):
        arches = [str(a) for a in rel.arch_list() if a]
    if not arches:
        arches = ["x86_64", "aarch64"]
    from oreon_build.services.rpmbuild_settle import schedule_side_tag_repodata_rebuild

    schedule_side_tag_repodata_rebuild(releasename, tag.name, arches)
    return {
        "names": wanted,
        "deleted_objects": len(keys),
        "jobs": len(job_ids),
    }


async def scrap_inactive_side_tags(db: Any) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=INACTIVE_DAYS)
    rows = (
        await db.execute(
            select(SideTag)
            .options(selectinload(SideTag.release))
            .where(SideTag.last_activity_at < cutoff)
        )
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for tag in rows:
        if await side_tag_is_busy(db, tag.id):
            continue
        releasename = getattr(tag.release, "releasename", None) or ""
        info = await scrap_side_tag(db, tag, releasename=str(releasename))
        out.append(info)
        logger.info("auto-scrapped inactive side tag %s (%s)", info["name"], info["id"])
    return out
