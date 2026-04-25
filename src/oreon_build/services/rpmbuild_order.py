"""Persistent rpmbuild-order plans with strict layer metadata."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select

from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import (
    BuildJob,
    BuildStatus,
    MockEnvironment,
    Package,
    PackageVersion,
    Release,
    RpmbuildPlan,
    RpmbuildPlanStatus,
    Source,
    Worker,
)
from oreon_build.services.build_chain import distgit_new_package_dict
from oreon_build.services.build_target_util import get_or_create_build_target, has_successful_build_for_target
from oreon_build.services.distgit_order import compute_distgit_build_order

logger = logging.getLogger(__name__)


@dataclass
class RpmbuildCreateResult:
    plan: RpmbuildPlan
    jobs: list[BuildJob]
    layers: list[list[str]]


def _default_distgit_url() -> str:
    return "https://github.com/oreonhq/rpm-specfiles.git"


async def _resolve_mock_for_pkg(db: Any, release_id: int, preferred_mock_environment_id: int | None) -> int | None:
    if preferred_mock_environment_id is not None:
        me = await db.get(MockEnvironment, int(preferred_mock_environment_id))
        if me is not None and me.release_id == release_id:
            return int(preferred_mock_environment_id)
    fb = await db.execute(
        select(MockEnvironment).where(MockEnvironment.release_id == release_id).order_by(MockEnvironment.id.asc()).limit(1)
    )
    one = fb.scalar_one_or_none()
    return int(one.id) if one is not None else None


async def _get_or_create_distgit_package(
    db: Any,
    *,
    releasename: str,
    package_name: str,
    distgit_url: str,
    distgit_branch: str,
    preferred_mock_environment_id: int | None,
) -> tuple[Package, int | None]:
    ex = await db.execute(select(Package).where(Package.name == package_name))
    pkg = ex.scalar_one_or_none()
    if pkg is not None:
        return pkg, pkg.preferred_mock_environment_id

    npd = distgit_new_package_dict(
        package_name,
        releasename,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        preferred_mock_environment_id=preferred_mock_environment_id,
    )
    pkg = Package(
        name=npd["name"],
        description=npd["description"],
        preferred_mock_environment_id=npd["preferred_mock_environment_id"],
        github_repository_id=npd["github_repository_id"],
        github_html_url=npd["github_html_url"],
    )
    db.add(pkg)
    await db.flush()
    pv = PackageVersion(package_id=pkg.id, version="0.0.1")
    db.add(pv)
    await db.flush()
    db.add(Source(package_version_id=pv.id, kind="distgit", url=npd["source_url"]))
    await db.flush()
    return pkg, pkg.preferred_mock_environment_id


async def enqueue_plan_layer_jobs(
    db: Any,
    *,
    plan: RpmbuildPlan,
    layer_index: int,
    architectures: list[str],
    priority: int,
    skip_complete_arches: bool = True,
    triggered_by_id: int | None = None,
) -> list[BuildJob]:
    layers = plan.layers_json if isinstance(plan.layers_json, list) else []
    if layer_index < 0 or layer_index >= len(layers):
        return []
    release = await db.get(Release, plan.release_id)
    if release is None:
        return []
    publish_branch = r2_publish_branch(release)
    jobs: list[BuildJob] = []
    layer_pkgs = [str(x) for x in layers[layer_index] if str(x).strip()]
    for pkg_name in layer_pkgs:
        pkg, pref = await _get_or_create_distgit_package(
            db,
            releasename=plan.releasename,
            package_name=pkg_name,
            distgit_url=plan.distgit_url,
            distgit_branch=plan.distgit_branch,
            preferred_mock_environment_id=None,
        )
        for arch in architectures:
            canon = normalize_machine_arch(arch)
            m_id = await _resolve_mock_for_pkg(db, release.id, pref)
            target = await get_or_create_build_target(db, release.id, canon, m_id)
            if skip_complete_arches and await has_successful_build_for_target(db, pkg.id, release.id, target.id, None):
                continue
            act = await db.execute(
                select(BuildJob.id)
                .where(
                    BuildJob.package_id == pkg.id,
                    BuildJob.release_id == release.id,
                    BuildJob.target_id == target.id,
                    BuildJob.rpmbuild_plan_id == plan.id,
                    BuildJob.rpmbuild_layer_index == layer_index,
                    BuildJob.status.in_((BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING, BuildStatus.SUCCESS)),
                )
                .limit(1)
            )
            if act.scalar_one_or_none() is not None:
                continue
            job = BuildJob(
                package_id=pkg.id,
                release_id=release.id,
                target_id=target.id,
                package_version_id=None,
                status=BuildStatus.PENDING,
                priority=priority,
                triggered_by_id=triggered_by_id,
                branch=publish_branch,
                rpmbuild_plan_id=plan.id,
                rpmbuild_layer_index=layer_index,
                rpmbuild_wave_index=0,
            )
            db.add(job)
            await db.flush()
            jobs.append(job)
    return jobs


async def create_rpmbuild_plan(
    db: Any,
    request: Any,
    *,
    release_id: int,
    repo_branch: str,
    seed_packages: list[str],
    architectures: list[str],
    priority: int,
    distgit_url: str | None = None,
    distgit_branch: str | None = None,
    triggered_by_id: int | None = None,
) -> RpmbuildCreateResult:
    release = await db.get(Release, release_id)
    if release is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    releasename = (release.releasename or "").strip()
    if not releasename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Release has no releasename")
    if not seed_packages:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="seed_packages must not be empty")

    workers = list((await db.execute(select(Worker))).scalars().all())
    worker_arches = {normalize_machine_arch(w.architecture) for w in workers if (w.architecture or "").strip()}
    selected_arches = [normalize_machine_arch(a) for a in architectures if (a or "").strip()]
    selected_arches = [a for a in selected_arches if a in worker_arches]
    if not selected_arches:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No selected architecture has an enrolled worker",
        )

    durl = (distgit_url or _default_distgit_url()).strip()
    dbranch = (distgit_branch or "main").strip()
    order = await compute_distgit_build_order(releasename=releasename, distgit_url=durl, distgit_branch=dbranch)
    if not order.layers:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No rpmbuild-order layers found for release")

    layer_map: dict[str, int] = {}
    for idx, layer in enumerate(order.layers):
        for pkg in layer:
            if pkg not in layer_map:
                layer_map[pkg] = idx

    clean_seed = sorted({x.strip() for x in seed_packages if x and x.strip()})
    missing = [x for x in clean_seed if x not in layer_map]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Seed packages not found in rpmbuild-order: {', '.join(missing[:20])}",
        )

    start_layer = min(layer_map[x] for x in clean_seed)
    plan = RpmbuildPlan(
        release_id=release.id,
        releasename=releasename,
        repo_branch=(repo_branch or "dev").strip() or "dev",
        distgit_url=durl,
        distgit_branch=dbranch,
        seed_mode="minimal_seed",
        seed_packages_json=clean_seed,
        layers_json=order.layers,
        status=RpmbuildPlanStatus.RUNNING,
        current_open_layer=start_layer,
    )
    db.add(plan)
    await db.flush()

    # Minimal seed bootstrap behavior: only queue seed packages in the opening layer.
    initial_jobs: list[BuildJob] = []
    for pkg_name in clean_seed:
        if layer_map[pkg_name] != start_layer:
            continue
        pkg, pref = await _get_or_create_distgit_package(
            db,
            releasename=releasename,
            package_name=pkg_name,
            distgit_url=durl,
            distgit_branch=dbranch,
            preferred_mock_environment_id=None,
        )
        for arch in selected_arches:
            target = await get_or_create_build_target(
                db,
                release.id,
                normalize_machine_arch(arch),
                await _resolve_mock_for_pkg(db, release.id, pref),
            )
            if await has_successful_build_for_target(db, pkg.id, release.id, target.id, None):
                continue
            job = BuildJob(
                package_id=pkg.id,
                release_id=release.id,
                target_id=target.id,
                package_version_id=None,
                status=BuildStatus.PENDING,
                priority=priority,
                triggered_by_id=triggered_by_id,
                branch=r2_publish_branch(release),
                rpmbuild_plan_id=plan.id,
                rpmbuild_layer_index=start_layer,
                rpmbuild_wave_index=0,
            )
            db.add(job)
            await db.flush()
            initial_jobs.append(job)

    await log_audit(
        db,
        "build.rpmbuild_plan_create",
        account_id=triggered_by_id,
        resource_type="rpmbuild_plan",
        resource_id=str(plan.id),
        details=f"release_id={release_id} seed_count={len(clean_seed)} start_layer={start_layer} queued={len(initial_jobs)}",
        ip_address=get_client_ip(request) if request is not None else None,
        user_agent=get_user_agent(request) if request is not None else "rpmbuild-plan",
    )

    return RpmbuildCreateResult(plan=plan, jobs=initial_jobs, layers=order.layers)
