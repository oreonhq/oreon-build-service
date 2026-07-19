from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from oreon_build.config import get_settings
from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.core.audit import log_audit
from oreon_build.models import (
    BuildJob,
    BuildStatus,
    MockEnvironment,
    Package,
    PackageVersion,
    Release,
    Source,
    Worker,
)
from oreon_build.services.build_target_util import (
    get_or_create_build_target,
    has_successful_build_for_target,
    latest_package_version_id,
)
from oreon_build.services.distgit_order import compute_distgit_build_order

logger = logging.getLogger(__name__)


async def _distgit_packages_for_release(
    db: Any, releasename: str
) -> list[tuple[int, str, int | None]]:
    rel = (releasename or "").strip()
    r = await db.execute(
        select(Package.id, Package.name, Package.preferred_mock_environment_id, Source.url)
        .join(PackageVersion, PackageVersion.package_id == Package.id)
        .join(Source, Source.package_version_id == PackageVersion.id)
        .where(Source.kind == "distgit", Source.url.isnot(None))
    )
    out: list[tuple[int, str, int | None]] = []
    seen: set[int] = set()
    for row in r.all():
        pid, name, p_mock, url = int(row[0]), str(row[1]), row[2], (row[3] or "")
        u = url.replace(" ", "")
        if rel and f"/{rel}/" not in u and f":{rel}/" not in u and f"#{rel}/" not in u:
            continue
        if pid in seen:
            continue
        seen.add(pid)
        out.append((pid, name, p_mock))
    return out


async def has_active_build(db: Any, package_id: int, release_id: int, target_id: int) -> bool:
    one = await db.execute(
        select(BuildJob.id)
        .where(
            BuildJob.package_id == package_id,
            BuildJob.release_id == release_id,
            BuildJob.target_id == target_id,
            BuildJob.status.in_((BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)),
        )
        .limit(1)
    )
    return one.scalar_one_or_none() is not None


def _predecessor_names_in_tree(order_edges: list[dict[str, str]], consumer: str) -> set[str]:
    return {e["to"] for e in order_edges if e.get("from") == consumer}


async def _resolve_mock_for_job(
    db: Any, release: Release, pkg_preferred: int | None, settings_id: int | None
) -> int | None:
    if settings_id is not None:
        me = await db.get(MockEnvironment, settings_id)
        if me is not None and me.release_id == release.id:
            return int(settings_id)
    if pkg_preferred is not None:
        me2 = await db.get(MockEnvironment, int(pkg_preferred))
        if me2 is not None and me2.release_id == release.id:
            return int(pkg_preferred)
    fb = await db.execute(
        select(MockEnvironment)
        .where(MockEnvironment.release_id == release.id)
        .order_by(MockEnvironment.id.asc())
        .limit(1)
    )
    f = fb.scalar_one_or_none()
    return f.id if f is not None else None


async def run_mass_rebuild_pass(db: Any) -> dict[str, Any]:
    s = get_settings()
    if not getattr(s, "mass_rebuild_enabled", False):
        return {"status": "disabled", "queued": 0, "details": []}

    max_n = max(1, int(getattr(s, "mass_rebuild_max_jobs_per_pass", 20) or 20))
    want_ids: set[int] = set()
    raw = (getattr(s, "mass_rebuild_release_ids", None) or "").strip()
    if raw:
        for p in raw.split(","):
            p = p.strip()
            if p.isdigit():
                want_ids.add(int(p))

    r_all = await db.execute(select(Release))
    releases = list(r_all.scalars().all())
    if want_ids:
        releases = [x for x in releases if x.id in want_ids]
    else:
        releases = [x for x in releases if bool(x.is_active)]

    workers = list((await db.execute(select(Worker))).scalars().all())
    w_arch: set[str] = {normalize_machine_arch(w.architecture) for w in workers if (w.architecture or "").strip()}

    arch_list = [normalize_machine_arch(a) for a in (getattr(s, "mass_rebuild_architectures", "") or "x86_64,aarch64").split(",") if (a or "").strip()]
    arch_list = [a for a in arch_list if a in w_arch]
    if not arch_list:
        return {"status": "no_workers", "queued": 0, "details": []}

    total_queued = 0
    details: list[dict[str, object]] = []
    dg = (getattr(s, "mass_rebuild_distgit_url", None) or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    br = (getattr(s, "mass_rebuild_distgit_branch", None) or "main").strip()
    settings_mock = getattr(s, "mass_rebuild_mock_environment_id", None)
    pr = int(getattr(s, "mass_rebuild_priority", 0) or 0)

    for release in releases:
        if total_queued >= max_n:
            break
        reln = (release.releasename or "").strip()
        if not reln:
            continue
        pkgs = await _distgit_packages_for_release(db, reln)
        by_name = {n: (pid, p_mock) for pid, n, p_mock in pkgs}
        if not by_name:
            continue
        try:
            order = await compute_distgit_build_order(releasename=reln, distgit_url=dg, distgit_branch=br)
        except Exception as e:
            logger.exception("distgit order %s: %s", reln, e)
            details.append({"release": reln, "error": str(e)[:300]})
            continue

        flat: list[str] = []
        layer_idx_by_name: dict[str, int] = {}
        for idx, layer in enumerate(order.layers):
            for n in layer:
                if n in by_name and n not in flat:
                    flat.append(n)
                if n in by_name and n not in layer_idx_by_name:
                    layer_idx_by_name[n] = idx

        from oreon_build.models import SideTagCreatedFrom
        from oreon_build.services.side_tags import bump_side_tag_activity, ensure_side_tag

        side_tag = await ensure_side_tag(
            db,
            release_id=release.id,
            name="mass-rebuild",
            owner_account_id=None,
            created_from=SideTagCreatedFrom.MANUAL,
        )
        await bump_side_tag_activity(db, side_tag.id)

        for name in flat:
            if total_queued >= max_n:
                break
            pid, pkg_preferred = by_name[name]
            pre = _predecessor_names_in_tree(order.edges, name)
            for arch in arch_list:
                if total_queued >= max_n:
                    break
                m_id = await _resolve_mock_for_job(db, release, pkg_preferred, settings_mock)
                target = await get_or_create_build_target(db, release.id, arch, m_id)
                pv_id = await latest_package_version_id(db, pid)
                if await has_successful_build_for_target(db, pid, release.id, target.id, pv_id):
                    continue
                if await has_active_build(db, pid, release.id, target.id):
                    continue
                ok = True
                for pred in pre:
                    if pred not in by_name:
                        continue
                    pida, p_pref = by_name[pred]
                    m2 = await _resolve_mock_for_job(db, release, p_pref, settings_mock)
                    t2 = await get_or_create_build_target(db, release.id, arch, m2)
                    pred_pv = await latest_package_version_id(db, pida)
                    if not await has_successful_build_for_target(
                        db, pida, release.id, t2.id, pred_pv
                    ):
                        ok = False
                        break
                if not ok:
                    continue
                job = BuildJob(
                    package_id=pid,
                    release_id=release.id,
                    target_id=target.id,
                    package_version_id=None,
                    status=BuildStatus.PENDING,
                    priority=pr,
                    triggered_by_id=None,
                    branch=side_tag.name,
                    side_tag_id=side_tag.id,
                    rpmbuild_layer_index=layer_idx_by_name.get(name),
                )
                db.add(job)
                await db.flush()
                total_queued += 1
                await log_audit(
                    db,
                    "build.mass_rebuild",
                    account_id=None,
                    resource_type="build_job",
                    resource_id=str(job.id),
                    details=f"release_id={release.id} arch={arch} package_id={pid} {name}",
                    ip_address=None,
                    user_agent="mass-rebuild",
                    emit_discord=False,
                )
                details.append({"job_id": job.id, "release": reln, "package": name, "arch": arch})

    return {"status": "ok", "queued": total_queued, "details": details}
