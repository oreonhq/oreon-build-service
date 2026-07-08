from __future__ import annotations

import bz2
import gzip
import logging
import lzma
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from oreon_build.config import get_settings
from oreon_build.models import BuildJob, BuildStatus, BuildTarget, Package, PublishTask, Release, RpmbuildPlan, RpmbuildPlanStatus
from oreon_build.services.r2 import get_r2_client
from oreon_build.services.rpmbuild_order import enqueue_plan_layer_jobs

logger = logging.getLogger(__name__)


def _decompress_if_needed(raw: bytes, key: str) -> bytes:
    lk = key.lower()
    if lk.endswith(".gz"):
        return gzip.decompress(raw)
    if lk.endswith(".bz2"):
        return bz2.decompress(raw)
    if lk.endswith(".xz"):
        return lzma.decompress(raw)
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
        layers = plan.layers_json if isinstance(plan.layers_json, list) else []
        if not layers:
            plan.status = RpmbuildPlanStatus.FAILED
            plan.error_message = "Plan has no layers"
            continue
        cur = int(plan.current_open_layer or 0)
        jobs, any_failed, publish_ready = await _layer_status(db, plan, cur)
        if any_failed:
            plan.status = RpmbuildPlanStatus.FAILED
            plan.error_message = f"Layer {cur} contains failed job(s)"
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
                architectures=rel.arch_list() or ["x86_64", "aarch64"],
                priority=0,
                triggered_by_id=None,
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
            architectures=rel.arch_list() or ["x86_64", "aarch64"],
            priority=0,
            triggered_by_id=None,
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
