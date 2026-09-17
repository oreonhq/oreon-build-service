from __future__ import annotations

import asyncio
import bz2
import gzip
import io
import logging
import lzma
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update

from oreon_build.config import get_settings
from oreon_build.models import BuildAttempt, BuildJob, BuildStatus, BuildTarget, Package, PublishTask, Release, RpmbuildPlan, RpmbuildPlanStatus
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.services.r2 import get_r2_client
from oreon_build.services.rpmbuild_order import enqueue_plan_layer_jobs

logger = logging.getLogger(__name__)


def rebuild_side_tag_repodata_sync(releasename: str, tag_name: str, arches: list[str]) -> None:
    from oreon_build.services.publisher import prefix_publish_lock, update_repodata_at_prefix
    from oreon_build.services.r2 import repo_r2_prefix

    for arch in arches:
        a = (arch or "").strip()
        if not a:
            continue
        prefix = repo_r2_prefix(releasename, tag_name, a)
        logger.info("rebuilding side tag repodata prefix=%s", prefix)
        with prefix_publish_lock(prefix):
            update_repodata_at_prefix(prefix, force_full=True)


def rewrite_distgit_source_url(old: str, distgit_url: str, distgit_branch: str) -> str | None:
    raw = (old or "").strip()
    if not distgit_url or not distgit_branch:
        return None
    frag = raw.rsplit("#", 1)[-1] if "#" in raw else ""
    path = frag.split(":", 1)[1].strip().lstrip("/") if ":" in frag else ""
    if not path:
        return None
    return f"{distgit_url.rstrip('/')}#{distgit_branch}:{path}"


async def _manual_chain_for_user(db: Any, user: Any, chain_plan_id: int) -> RpmbuildPlan:
    from fastapi import HTTPException, status
    from oreon_build.models import SideTag
    from oreon_build.services.side_tags import get_side_tag_by_name, require_side_tag_access

    plan = await db.get(RpmbuildPlan, chain_plan_id)
    if plan is None or plan.seed_mode not in ("manual_chain", "discovery"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chain build not found")
    tag: SideTag | None = await get_side_tag_by_name(db, plan.release_id, plan.repo_branch)
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chain side tag not found")
    require_side_tag_access(user, tag)
    return plan


async def cancel_chain_plan_by_user(db: Any, request: Any, user: Any, chain_plan_id: int) -> RpmbuildPlan:
    from fastapi import HTTPException, status

    plan = await _manual_chain_for_user(db, user, chain_plan_id)
    if plan.status not in (RpmbuildPlanStatus.RUNNING, RpmbuildPlanStatus.WAITING_SETTLE):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Chain is already {plan.status.value}")
    plan.status = RpmbuildPlanStatus.CANCELLED
    plan.error_message = "Cancelled by user"
    open_statuses = (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.BLOCKED, BuildStatus.RUNNING)
    await db.execute(
        update(BuildJob)
        .where(BuildJob.rpmbuild_plan_id == plan.id, BuildJob.status.in_(open_statuses))
        .values(status=BuildStatus.CANCELLED, completed_at=datetime.now(timezone.utc), blocked_reason="Chain cancelled by user")
    )
    await db.execute(
        update(BuildAttempt)
        .where(
            BuildAttempt.build_job_id.in_(select(BuildJob.id).where(BuildJob.rpmbuild_plan_id == plan.id)),
            BuildAttempt.status.in_(open_statuses),
        )
        .values(status=BuildStatus.CANCELLED, finished_at=datetime.now(timezone.utc), error_message="Chain cancelled by user")
    )
    now = datetime.now(timezone.utc)
    await db.execute(
        update(PublishTask)
        .where(
            PublishTask.build_job_id.in_(select(BuildJob.id).where(BuildJob.rpmbuild_plan_id == plan.id)),
            PublishTask.status == "pending",
        )
        .values(status="cancelled", finished_at=now, error_message="Chain cancelled by user")
    )
    await log_audit(db, "build.chain_cancel", account_id=user.id, resource_type="rpmbuild_plan", resource_id=str(plan.id), ip_address=get_client_ip(request), user_agent=get_user_agent(request))
    return plan


def schedule_side_tag_repodata_rebuild(releasename: str, tag_name: str, arches: list[str]) -> None:
    task = asyncio.create_task(
        asyncio.to_thread(
            rebuild_side_tag_repodata_sync,
            releasename,
            tag_name,
            [str(a) for a in arches],
        )
    )

    def _done(t: asyncio.Task[None]) -> None:
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.exception("background side tag repodata rebuild failed tag=%s", tag_name, exc_info=exc)

    task.add_done_callback(_done)


async def resume_chain_plan_by_user(db: Any, request: Any, user: Any, chain_plan_id: int) -> RpmbuildPlan:
    from fastapi import HTTPException, status

    plan = await _manual_chain_for_user(db, user, chain_plan_id)
    if plan.status == RpmbuildPlanStatus.SUCCESS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Chain already succeeded")
    cur = int(plan.current_open_layer or 0)
    jobs = list((await db.execute(select(BuildJob).where(BuildJob.rpmbuild_plan_id == plan.id, BuildJob.rpmbuild_layer_index == cur))).scalars().all())
    reopen = [j for j in jobs if j.status in (BuildStatus.FAILED, BuildStatus.CANCELLED, BuildStatus.BLOCKED)]
    if not reopen:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No failed or cancelled jobs in the current chain group")
    from oreon_build.services.build_target_util import latest_package_version_id
    from oreon_build.models import PackageVersion, Source

    for job in reopen:
        # Chain resume reuses the existing job row. Refresh its pinned source
        # version so a spec release bump is picked up by the next attempt.
        current_version_id = await latest_package_version_id(db, int(job.package_id))
        if current_version_id is not None:
            job.package_version_id = current_version_id
        # Keep the package's DistGit source aligned with this chain's inputs.
        # Existing packages may retain a source URL from an older chain.
        if plan.distgit_url and plan.distgit_branch:
            rows = await db.execute(
                select(Source)
                .join(PackageVersion, Source.package_version_id == PackageVersion.id)
                .where(PackageVersion.package_id == job.package_id, Source.kind == "distgit")
            )
            for src in rows.scalars().all():
                rewritten = rewrite_distgit_source_url(
                    src.url or "", plan.distgit_url, plan.distgit_branch
                )
                if rewritten:
                    src.url = rewritten
        job.status = BuildStatus.PENDING
        job.completed_at = None
        job.blocked_reason = None
        job.error_message = None
    now = datetime.now(timezone.utc)
    reopen_ids = [j.id for j in reopen]
    await db.execute(
        update(BuildAttempt)
        .where(
            BuildAttempt.build_job_id.in_(reopen_ids),
            BuildAttempt.status.in_((BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)),
        )
        .values(
            status=BuildStatus.CANCELLED,
            finished_at=now,
            error_message="Superseded by chain resume",
        )
    )
    plan.status = RpmbuildPlanStatus.RUNNING
    plan.error_message = None
    plan.settle_deadline_at = None
    await log_audit(db, "build.chain_resume", account_id=user.id, resource_type="rpmbuild_plan", resource_id=str(plan.id), details=f"reopened {len(reopen)} job(s) in group {cur + 1}", ip_address=get_client_ip(request), user_agent=get_user_agent(request))
    return plan


def _decompress_if_needed(raw: bytes, key: str) -> bytes:
    lk = key.lower()
    if lk.endswith(".gz"):
        return gzip.decompress(raw)
    if lk.endswith(".bz2"):
        return bz2.decompress(raw)
    if lk.endswith(".xz"):
        return lzma.decompress(raw)
    if lk.endswith(".zst"):
        import zstandard as zstd

        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(io.BytesIO(raw)) as reader:
            return reader.read()
    return raw


def _resolve_primary_key(repo_prefix: str, repomd_raw: bytes) -> str | None:
    try:
        root = ET.fromstring(repomd_raw)
    except ET.ParseError:
        return None
    for data in root.findall(".//{*}data"):
        if (data.attrib.get("type") or "") != "primary":
            continue
        loc = data.find("{*}location")
        href = (loc.attrib.get("href") if loc is not None else "") or ""
        href = href.strip()
        if not href:
            continue
        if href.startswith(f"{repo_prefix}/"):
            return href
        return f"{repo_prefix}/{href.lstrip('/')}"
    return None


def _known_sources_from_primary(primary_xml: bytes) -> set[str]:
    out: set[str] = set()
    try:
        root = ET.fromstring(primary_xml)
    except ET.ParseError:
        return out
    for pkg in root.findall(".//{*}package"):
        arch = (pkg.findtext("{*}arch") or "").strip()
        if arch == "src":
            continue
        sourcerpm = (pkg.findtext(".//{*}sourcerpm") or "").strip()
        if sourcerpm.endswith(".src.rpm"):
            s = sourcerpm[:-8]
            parts = s.rsplit("-", 2)
            if len(parts) == 3 and parts[0]:
                out.add(parts[0])
        name = (pkg.findtext("{*}name") or "").strip()
        if name:
            out.add(name)
    return out


def _sources_visible_in_arch(releasename: str, branch: str, arch: str) -> set[str]:
    r2 = get_r2_client()
    repo_prefix = f"{releasename}/{branch}/{arch}"
    repomd_key = f"{repo_prefix}/repodata/repomd.xml"
    repomd = r2.get_object(repomd_key)
    if not repomd:
        return set()
    primary_key = _resolve_primary_key(repo_prefix, repomd)
    if not primary_key:
        return set()
    primary_raw = r2.get_object(primary_key)
    if not primary_raw:
        return set()
    primary_xml = _decompress_if_needed(primary_raw, primary_key)
    return _known_sources_from_primary(primary_xml)


async def _layer_status(db: Any, plan: RpmbuildPlan, layer: int) -> tuple[list[BuildJob], bool, bool]:
    q = await db.execute(
        select(BuildJob).where(
            BuildJob.rpmbuild_plan_id == plan.id,
            BuildJob.rpmbuild_layer_index == layer,
        )
    )
    jobs = list(q.scalars().all())
    if not jobs:
        return [], False, False
    any_failed = any(j.status in (BuildStatus.FAILED, BuildStatus.CANCELLED) for j in jobs)
    all_success = all(j.status == BuildStatus.SUCCESS for j in jobs)
    if not all_success:
        return jobs, any_failed, False
    pub_ok = True
    for j in jobs:
        att = await db.execute(
            select(PublishTask).where(PublishTask.build_job_id == j.id).order_by(PublishTask.id.desc()).limit(1)
        )
        p = att.scalar_one_or_none()
        if p is None or p.status != "success":
            pub_ok = False
            break
    return jobs, any_failed, pub_ok


async def advance_rpmbuild_plans_once(db: Any) -> None:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    rows = await db.execute(
        select(RpmbuildPlan)
        .where(RpmbuildPlan.status.in_((RpmbuildPlanStatus.RUNNING, RpmbuildPlanStatus.WAITING_SETTLE)))
        .with_for_update(skip_locked=True)
    )
    plans = list(rows.scalars().all())
    if not plans:
        return

    for plan in plans:
        options = plan.options_json if isinstance(plan.options_json, dict) else {}
        plan_arches = options.get("architectures") or []
        plan_priority = int(options.get("priority") or 0)
        plan_mock_id = options.get("preferred_mock_environment_id")
        layers = plan.layers_json if isinstance(plan.layers_json, list) else []
        if not layers:
            plan.status = RpmbuildPlanStatus.FAILED
            plan.error_message = "Plan has no layers"
            continue
        cur = int(plan.current_open_layer or 0)
        jobs, any_failed, publish_ready = await _layer_status(db, plan, cur)
        if any_failed:
            # Let other packages in the same parallel group finish before the
            # chain becomes terminal. Later groups remain unopened.
            if any(j.status in (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.BLOCKED, BuildStatus.RUNNING) for j in jobs):
                plan.status = RpmbuildPlanStatus.RUNNING
                continue
            plan.status = RpmbuildPlanStatus.FAILED
            label = "Group" if plan.seed_mode == "manual_chain" else "Layer"
            plan.error_message = f"{label} {cur + 1 if plan.seed_mode == 'manual_chain' else cur} contains failed job(s)"
            continue
        if not jobs:
            rel = await db.get(Release, plan.release_id)
            if rel is None:
                plan.status = RpmbuildPlanStatus.FAILED
                plan.error_message = "Release missing"
                continue
            await enqueue_plan_layer_jobs(
                db,
                plan=plan,
                layer_index=cur,
                architectures=plan_arches or rel.arch_list() or ["x86_64", "aarch64"],
                priority=plan_priority,
                triggered_by_id=None,
                preferred_mock_environment_id=plan_mock_id,
            )
            continue
        if not publish_ready:
            plan.status = RpmbuildPlanStatus.RUNNING
            continue

        rel = await db.get(Release, plan.release_id)
        if rel is None:
            plan.status = RpmbuildPlanStatus.FAILED
            plan.error_message = "Release missing"
            continue
        pkg_names_by_job: dict[int, str] = {}
        if jobs:
            pids = sorted({j.package_id for j in jobs})
            prs = await db.execute(select(Package.id, Package.name).where(Package.id.in_(pids)))
            pkg_names_by_job = {int(pid): str(name) for pid, name in prs.all()}

        per_arch_need: dict[str, set[str]] = {}
        for j in jobs:
            if j.target_id is None:
                continue
            tr = await db.execute(select(BuildTarget.architecture).where(BuildTarget.id == j.target_id))
            arch = tr.scalar_one_or_none()
            if not arch:
                continue
            per_arch_need.setdefault(str(arch), set()).add(pkg_names_by_job.get(j.package_id, ""))

        repodata_ready = True
        for arch, needed in per_arch_need.items():
            visible = _sources_visible_in_arch(rel.releasename, plan.repo_branch, arch)
            for n in needed:
                if n and n not in visible:
                    repodata_ready = False
                    break
            if not repodata_ready:
                break

        if not repodata_ready:
            plan.status = RpmbuildPlanStatus.WAITING_SETTLE
            if plan.settle_deadline_at is None:
                plan.settle_deadline_at = now + timedelta(seconds=settings.rpmbuild_settle_timeout_seconds)
            elif now >= plan.settle_deadline_at:
                plan.status = RpmbuildPlanStatus.FAILED
                plan.error_message = f"Timed out waiting for repodata settle at layer {cur}"
            continue

        plan.settle_deadline_at = None
        next_layer = cur + 1
        if next_layer >= len(layers):
            plan.status = RpmbuildPlanStatus.SUCCESS
            continue
        plan.current_open_layer = next_layer
        plan.status = RpmbuildPlanStatus.RUNNING
        await enqueue_plan_layer_jobs(
            db,
            plan=plan,
            layer_index=next_layer,
            architectures=plan_arches or rel.arch_list() or ["x86_64", "aarch64"],
            priority=plan_priority,
            triggered_by_id=None,
            preferred_mock_environment_id=plan_mock_id,
        )
        logger.info("rpmbuild plan advanced id=%s layer=%s", plan.id, next_layer)


async def on_repo_publish_event_for_rpmbuild(attempt_id: int) -> None:
    from oreon_build.db.session import async_session_maker
    from oreon_build.models import BuildAttempt

    async with async_session_maker() as db:
        att = await db.get(BuildAttempt, attempt_id)
        if att is None:
            return
        job = await db.get(BuildJob, att.build_job_id)
        if job is None or job.rpmbuild_plan_id is None:
            return
        await advance_rpmbuild_plans_once(db)
        await db.commit()
