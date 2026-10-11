from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select

from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.machine_arch import normalize_machine_arch
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
from oreon_build.services.distgit_order import compute_distgit_build_order, db_safe_spec_evr
from oreon_build.core.repo_branch import release_distgit_branch
from oreon_build.services.build_target_util import (
    get_or_create_build_target,
    has_successful_build_for_target,
    latest_package_version_id,
    resolve_package_by_name,
)

logger = logging.getLogger(__name__)

_CHAIN_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*$")


def distgit_new_package_dict(
    pkg: str,
    releasename: str,
    *,
    distgit_url: str,
    distgit_branch: str,
    preferred_mock_environment_id: int | None,
    spec_version: str | None = None,
) -> dict[str, Any]:
    spec_rel = f"{releasename}/{pkg}/{pkg}.spec"
    url = f"{distgit_url.rstrip('/')}#{distgit_branch}:{spec_rel}"
    return {
        "name": pkg,
        "description": None,
        "preferred_mock_environment_id": preferred_mock_environment_id,
        "github_repository_id": None,
        "github_html_url": "https://github.com/oreonhq/rpm-specfiles",
        "source_type": "distgit",
        "source_url": url,
        "source_urls": None,
        "source_branch": distgit_branch,
        "source_path": spec_rel,
        "version": db_safe_spec_evr((spec_version or "0.0.1").strip() or "0.0.1"),
    }


def parse_manual_chain(chain: str) -> list[list[str]]:
    """Parse Koji-style ``a b : c : d e`` groups without graph expansion.

    The same package may show up in a later group. That earlier copy is a
    bootstrap build. Twice in one group is still invalid.
    """
    raw_groups = chain.split(":")
    if any(not group.strip() for group in raw_groups):
        raise ValueError("Chain contains an empty group")
    layers: list[list[str]] = []
    for raw_group in raw_groups:
        group: list[str] = []
        seen: set[str] = set()
        for name in raw_group.replace(",", " ").split():
            if not _CHAIN_PACKAGE_RE.fullmatch(name):
                raise ValueError(f"Invalid package name in chain: {name}")
            if name in seen:
                raise ValueError(f"Package appears more than once in the same group: {name}")
            seen.add(name)
            group.append(name)
        layers.append(group)
    return layers


async def _dep_providers_for_names(
    *,
    releasename: str,
    distgit_url: str,
    distgit_branch: str,
    names: set[str],
) -> dict[str, list[str]]:
    from oreon_build.services.distgit_order import compute_distgit_build_order_for_chain

    if not names:
        return {}
    try:
        order = await compute_distgit_build_order_for_chain(
            releasename=releasename,
            distgit_url=distgit_url,
            distgit_branch=distgit_branch,
            root_packages=sorted(names),
        )
    except Exception:
        logger.exception("chain dep lookup failed for %s", releasename)
        return {}
    return dep_providers_from_edges(order.edges, names)


def dep_providers_from_edges(edges: list[dict], names: set[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {n: [] for n in names}
    for edge in edges or []:
        if not isinstance(edge, dict):
            continue
        cons = str(edge.get("from") or "").strip()
        dep = str(edge.get("to") or "").strip()
        if cons in names and dep in names and dep != cons and dep not in out[cons]:
            out[cons].append(dep)
    return out


def names_blocked_by_failures(
    layers: list[list[str]],
    failed: set[str],
    providers: dict[str, list[str]],
) -> set[str]:
    """Packages that cannot build because a dependency failed, directly or through another skip."""
    blocked = set(failed)
    names = [str(n) for group in layers for n in group]
    changed = True
    while changed:
        changed = False
        for name in names:
            if name in blocked:
                continue
            if any(dep in blocked for dep in providers.get(name, [])):
                blocked.add(name)
                changed = True
    return blocked - set(failed)


def bootstrap_names_by_layer(layers: list[list[str]]) -> dict[int, set[str]]:
    """Names whose later copy is the real build. Earlier copies are bootstrap."""
    last: dict[str, int] = {}
    for idx, group in enumerate(layers):
        for name in group:
            last[name] = idx
    out: dict[int, set[str]] = {}
    for idx, group in enumerate(layers):
        boots = {name for name in group if last.get(name) != idx}
        if boots:
            out[idx] = boots
    return out


def _plan_skip_jobs_not_before(plan: RpmbuildPlan) -> datetime | None:
    ca = plan.created_at
    if ca is None:
        return None
    if ca.tzinfo is None:
        return ca.replace(tzinfo=timezone.utc)
    return ca


@dataclass
class RpmbuildCreateResult:
    plan: RpmbuildPlan
    jobs: list[BuildJob]
    layers: list[list[str]]


def _default_distgit_url() -> str:
    return "https://github.com/oreonhq/rpm-specfiles.git"


async def _resolve_mock_for_pkg(db: Any, release_id: int, preferred_mock_environment_id: int | None) -> int | None:
    from oreon_build.services.build_target_util import pick_mock_environment_id_for_release

    return await pick_mock_environment_id_for_release(
        db, release_id, preferred_id=preferred_mock_environment_id
    )


async def _get_or_create_distgit_package(
    db: Any,
    *,
    releasename: str,
    package_name: str,
    distgit_url: str,
    distgit_branch: str,
    preferred_mock_environment_id: int | None,
    release_id: int | None = None,
) -> tuple[Package, int | None]:
    pkg = await resolve_package_by_name(db, package_name, release_id=release_id)
    if pkg is not None:
        # Reused packages must follow the current chain inputs. Previously
        # this early return left an old DistGit URL/source association in place.
        pv_id = await latest_package_version_id(db, pkg.id)
        if pv_id is not None:
            want = distgit_new_package_dict(
                package_name,
                releasename,
                distgit_url=distgit_url,
                distgit_branch=distgit_branch,
                preferred_mock_environment_id=preferred_mock_environment_id,
            )["source_url"]
            src_r = await db.execute(
                select(Source)
                .join(PackageVersion, Source.package_version_id == PackageVersion.id)
                .where(
                    PackageVersion.package_id == pkg.id,
                    Source.kind == "distgit",
                )
            )
            rows = list(src_r.scalars().all())
            if not rows:
                db.add(Source(package_version_id=pv_id, kind="distgit", url=want))
            else:
                for src in rows:
                    src.url = want
            await db.flush()
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
    preferred_mock_environment_id: int | None = None,
    skip_package_names: set[str] | None = None,
) -> list[BuildJob]:
    layers = plan.layers_json if isinstance(plan.layers_json, list) else []
    if layer_index < 0 or layer_index >= len(layers):
        return []
    release = await db.get(Release, plan.release_id)
    if release is None:
        return []
    from oreon_build.models import SideTagCreatedFrom
    from oreon_build.services.side_tags import (
        bump_side_tag_activity,
        ensure_side_tag,
        is_primary_repo_branch,
        sanitize_side_tag_name,
    )

    tag_name = sanitize_side_tag_name(plan.repo_branch or "")
    if not tag_name or is_primary_repo_branch(tag_name):
        tag_name = "rpmbuild-plan"
    side_tag = await ensure_side_tag(
        db,
        release_id=release.id,
        name=tag_name,
        owner_account_id=triggered_by_id,
        created_from=SideTagCreatedFrom.MANUAL,
    )
    publish_branch = side_tag.name
    await bump_side_tag_activity(db, side_tag.id)
    jobs: list[BuildJob] = []
    layer_pkgs = [str(x) for x in layers[layer_index] if str(x).strip()]
    skip_names = skip_package_names or set()
    boot_names = bootstrap_names_by_layer([[str(x) for x in g] for g in layers]).get(layer_index, set())
    plan_not_before = _plan_skip_jobs_not_before(plan)
    for pkg_name in layer_pkgs:
        pkg, _pref = await _get_or_create_distgit_package(
            db,
            releasename=plan.releasename,
            package_name=pkg_name,
            distgit_url=plan.distgit_url,
            distgit_branch=plan.distgit_branch,
            preferred_mock_environment_id=None,
            release_id=release.id,
        )
        for arch in architectures:
            canon = normalize_machine_arch(arch)
            m_id = await _resolve_mock_for_pkg(
                db,
                release.id,
                preferred_mock_environment_id,
            )
            target = await get_or_create_build_target(db, release.id, canon, m_id)
            pv_id = await latest_package_version_id(db, pkg.id)
            is_bootstrap = pkg_name in boot_names
            if skip_complete_arches and not is_bootstrap and await has_successful_build_for_target(
                db,
                pkg.id,
                release.id,
                target.id,
                pv_id,
                jobs_created_on_or_after=plan_not_before,
                ignore_bootstrap=True,
            ):
                continue
            act = await db.execute(
                select(BuildJob.id)
                .where(
                    BuildJob.package_id == pkg.id,
                    BuildJob.release_id == release.id,
                    BuildJob.target_id == target.id,
                    BuildJob.rpmbuild_plan_id == plan.id,
                    BuildJob.rpmbuild_layer_index == layer_index,
                    BuildJob.status.in_((
                        BuildStatus.PENDING,
                        BuildStatus.QUEUED,
                        BuildStatus.RUNNING,
                        BuildStatus.SUCCESS,
                        BuildStatus.SKIPPED,
                    )),
                )
                .limit(1)
            )
            if act.scalar_one_or_none() is not None:
                continue
            job = BuildJob(
                package_id=pkg.id,
                release_id=release.id,
                target_id=target.id,
                package_version_id=pv_id,
                status=BuildStatus.PENDING,
                priority=priority,
                triggered_by_id=triggered_by_id,
                branch=publish_branch,
                side_tag_id=side_tag.id,
                rpmbuild_plan_id=plan.id,
                rpmbuild_layer_index=layer_index,
                rpmbuild_wave_index=0,
            )
            if pkg_name in skip_names:
                job.status = BuildStatus.SKIPPED
                job.error_message = "Skipped, a dependency in this chain failed"
            elif is_bootstrap:
                job.job_options = {
                    "build_profile": "bootstrap",
                    "rpmbuild_defines": {"bootstrap": "1"},
                }
            db.add(job)
            await db.flush()
            jobs.append(job)
    return jobs


async def create_manual_chain_plan(
    db: Any,
    request: Any,
    *,
    chain: str,
    release_id: int,
    side_tag_id: int,
    architectures: list[str],
    priority: int = 0,
    preferred_mock_environment_id: int | None = None,
    distgit_url: str | None = None,
    distgit_branch: str | None = None,
    triggered_by_id: int | None = None,
) -> RpmbuildCreateResult:
    from oreon_build.models import SideTag

    try:
        layers = parse_manual_chain(chain)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    release = await db.get(Release, release_id)
    if release is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    if not release.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Release is inactive")
    side_tag = await db.get(SideTag, side_tag_id)
    if side_tag is None or side_tag.release_id != release_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Side tag does not belong to release")
    workers = list((await db.execute(select(Worker))).scalars().all())
    worker_arches = {normalize_machine_arch(w.architecture) for w in workers if (w.architecture or "").strip()}
    selected_arches = list(dict.fromkeys(normalize_machine_arch(a) for a in architectures if (a or "").strip()))
    unavailable = [a for a in selected_arches if a not in worker_arches]
    if not selected_arches or unavailable:
        detail = "No architectures selected" if not selected_arches else f"No enrolled worker for: {', '.join(unavailable)}"
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    if preferred_mock_environment_id is not None:
        mock = await db.get(MockEnvironment, preferred_mock_environment_id)
        if mock is None or mock.release_id != release_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mock environment does not belong to release")

    durl = (distgit_url or _default_distgit_url()).strip()
    dbranch = (distgit_branch or release_distgit_branch(release)).strip()
    flat = [name for group in layers for name in group]
    plan = RpmbuildPlan(
        release_id=release.id,
        releasename=release.releasename,
        repo_branch=side_tag.name,
        distgit_url=durl,
        distgit_branch=dbranch,
        seed_mode="manual_chain",
        seed_packages_json=flat,
        layers_json=layers,
        options_json={
            "architectures": selected_arches,
            "priority": int(priority),
            "preferred_mock_environment_id": preferred_mock_environment_id,
            "dep_providers": await _dep_providers_for_names(
                releasename=release.releasename,
                distgit_url=durl,
                distgit_branch=dbranch,
                names=set(flat),
            ),
        },
        status=RpmbuildPlanStatus.RUNNING,
        current_open_layer=0,
    )
    db.add(plan)
    await db.flush()
    from oreon_build.services.side_tags import bump_side_tag_activity

    await bump_side_tag_activity(db, side_tag.id)
    jobs = await enqueue_plan_layer_jobs(
        db,
        plan=plan,
        layer_index=0,
        architectures=selected_arches,
        priority=priority,
        skip_complete_arches=False,
        triggered_by_id=triggered_by_id,
        preferred_mock_environment_id=preferred_mock_environment_id,
    )
    await log_audit(
        db,
        "build.chain_trigger",
        account_id=triggered_by_id,
        resource_type="rpmbuild_plan",
        resource_id=str(plan.id),
        details=f"release_id={release_id} groups={len(layers)} packages={len(flat)} queued={len(jobs)}",
        ip_address=get_client_ip(request) if request is not None else None,
        user_agent=get_user_agent(request) if request is not None else "chain-build",
    )
    return RpmbuildCreateResult(plan=plan, jobs=jobs, layers=layers)


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
    from oreon_build.models import SideTagCreatedFrom
    from oreon_build.services.side_tags import (
        bump_side_tag_activity,
        ensure_side_tag,
        is_primary_repo_branch,
        sanitize_side_tag_name,
    )

    tag_name = sanitize_side_tag_name(repo_branch or "")
    if not tag_name or is_primary_repo_branch(tag_name):
        tag_name = "rpmbuild-plan"
    side_tag = await ensure_side_tag(
        db,
        release_id=release.id,
        name=tag_name,
        owner_account_id=triggered_by_id,
        created_from=SideTagCreatedFrom.MANUAL,
    )
    await bump_side_tag_activity(db, side_tag.id)
    plan = RpmbuildPlan(
        release_id=release.id,
        releasename=releasename,
        repo_branch=side_tag.name,
        distgit_url=durl,
        distgit_branch=dbranch,
        seed_mode="minimal_seed",
        seed_packages_json=clean_seed,
        layers_json=order.layers,
        options_json={
            "dep_providers": dep_providers_from_edges(
                order.edges,
                {str(n) for layer in order.layers for n in layer},
            ),
        },
        status=RpmbuildPlanStatus.RUNNING,
        current_open_layer=start_layer,
    )
    db.add(plan)
    await db.flush()
    await db.refresh(plan)

    initial_jobs: list[BuildJob] = []
    for pkg_name in clean_seed:
        if layer_map[pkg_name] != start_layer:
            continue
        pkg, _pref = await _get_or_create_distgit_package(
            db,
            releasename=releasename,
            package_name=pkg_name,
            distgit_url=durl,
            distgit_branch=dbranch,
            preferred_mock_environment_id=None,
            release_id=release.id,
        )
        for arch in selected_arches:
            target = await get_or_create_build_target(
                db,
                release.id,
                normalize_machine_arch(arch),
                await _resolve_mock_for_pkg(db, release.id, None),
            )
            pv_id = await latest_package_version_id(db, pkg.id)
            if await has_successful_build_for_target(
                db,
                pkg.id,
                release.id,
                target.id,
                pv_id,
                jobs_created_on_or_after=_plan_skip_jobs_not_before(plan),
            ):
                continue
            job = BuildJob(
                package_id=pkg.id,
                release_id=release.id,
                target_id=target.id,
                package_version_id=None,
                status=BuildStatus.PENDING,
                priority=priority,
                triggered_by_id=triggered_by_id,
                branch=side_tag.name,
                side_tag_id=side_tag.id,
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
