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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field
from oreon_build.db.session import init_db, async_session_maker
from oreon_build.core.auth import require_admin, require_account
from oreon_build.api.routers.auth import router as auth_router
from oreon_build.models import (
    Artifact,
    CveMatch,
    CveMatchRelease,
    Release,
    SecurityAdvisory,
    SecurityAdvisoryRelease,
    TrackedRpm,
)
from oreon_build.config import get_settings as get_app_settings
from oreon_build.services.discord import send_security_discord_embed
from oreon_build.watchdog.package_names import (
    parse_omit_subpackage_suffixes,
    tracked_rpm_not_subpackage_predicate,
)

from .scanner import scan_cves_once

logger = logging.getLogger(__name__)


CVE_RESOLVE_REASONS = frozenset(
    {"backport", "upstream_bump", "false_positive", "mitigated", "accepted_risk", "other"}
)


def _configure_watchdog_logging() -> None:
    root = logging.getLogger("oreon_build")
    root.setLevel(logging.INFO)
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        root.addHandler(h)
    root.propagate = False


class CveResolveBody(BaseModel):
    """Optional metadata when marking a CVE row handled (e.g. distro backport)."""
    reason: str | None = Field(None, max_length=32)
    notes: str | None = Field(None, max_length=4000)


class CustomAdvisoryCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    cve_id: str | None = Field(None, max_length=64)
    package_name: str = Field(..., min_length=1, max_length=256)
    package_version: str = Field(..., min_length=1, max_length=128)
    severity: str | None = Field(None, max_length=32)
    upstream_url: str | None = Field(None, max_length=1024)
    summary: str | None = Field(None)
    recommended_fix: str | None = Field(None)
    affected_release_ids: list[int] = Field(..., min_length=1)


def _dt_to_iso(dt) -> str | None:
    # Starlette/JSONResponse can’t serialize datetime directly.
    if not dt:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    _configure_watchdog_logging()

    from oreon_build.config import get_settings

    settings = get_settings()
    interval_s = max(60, int(getattr(settings, "cve_scan_interval_minutes", 30) * 60))
    app.state.scan_lock = asyncio.Lock()

    async def _run_scan(reason: str) -> None:
        try:
            async with app.state.scan_lock:
                logger.info("Watchdog inventory sync started reason=%s", reason)
                await scan_cves_once()
                logger.info("Watchdog inventory sync finished reason=%s", reason)
        except Exception:
            logger.exception("Watchdog inventory sync failed reason=%s", reason)

    async def _periodic_loop() -> None:
        # Guaranteed startup scan followed by interval scans.
        await _run_scan("startup")
        while True:
            await asyncio.sleep(interval_s)
            await _run_scan("schedule")

    periodic_task = asyncio.create_task(_periodic_loop())

    try:
        yield
    finally:
        periodic_task.cancel()
        try:
            await periodic_task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    app = FastAPI(title="Oreon Watchdog", version="1.0.0", lifespan=lifespan)

    web_dir = Path(__file__).resolve().parent.parent.parent.parent / "web"
    if not web_dir.exists():
        raise RuntimeError(f"web directory not found: {web_dir}")

    app.mount("/static", StaticFiles(directory=str(web_dir / "static")), name="static")

    # Reuse the main API login endpoint on this port so the watchdog dashboard
    # can be "public view, auth-gated data" without cross-service requests.
    app.include_router(auth_router, prefix="/api")

    @app.get("/")
    async def _index():
        return FileResponse(web_dir / "watchdog.html")

    @app.get("/advisories/cve/{cve_match_id}")
    async def _advisory_cve(cve_match_id: int):
        return FileResponse(web_dir / "advisory-cve.html")

    @app.get("/advisories/custom/{advisory_id}")
    async def _advisory_custom(advisory_id: int):
        return FileResponse(web_dir / "advisory-custom.html")

    @app.get("/api/security/advisories")
    async def list_advisories(
        release_id: int | None = Query(None),
        package_name: str | None = Query(None),
        omit_subpackages: bool = Query(True),
        limit: int = Query(25, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        from datetime import datetime, timezone
        from sqlalchemy import and_, func, select

        k = offset + limit
        st = get_app_settings()
        sp_suf = parse_omit_subpackage_suffixes(str(st.cve_scan_omit_subpackage_suffixes or ""))
        sp_pred = tracked_rpm_not_subpackage_predicate(sp_suf) if omit_subpackages and sp_suf else None

        async with async_session_maker() as db:
            # ---- totals (distinct IDs to avoid duplicates from joins)
            cve_count_q = (
                select(func.count(func.distinct(CveMatch.id)))
                .select_from(CveMatch)
                .join(TrackedRpm, CveMatch.tracked_rpm_id == TrackedRpm.id)
            )
            if release_id is not None:
                cve_count_q = cve_count_q.join(CveMatchRelease, CveMatchRelease.cve_match_id == CveMatch.id).where(
                    CveMatchRelease.release_id == release_id
                )
            if package_name:
                cve_count_q = cve_count_q.where(TrackedRpm.package_name == package_name)
            if sp_pred is not None:
                cve_count_q = cve_count_q.where(sp_pred)

            custom_count_q = select(func.count(func.distinct(SecurityAdvisory.id))).select_from(SecurityAdvisory)
            if release_id is not None:
                custom_count_q = custom_count_q.join(
                    SecurityAdvisoryRelease, SecurityAdvisoryRelease.advisory_id == SecurityAdvisory.id
                ).where(SecurityAdvisoryRelease.release_id == release_id)
            if package_name:
                custom_count_q = custom_count_q.where(SecurityAdvisory.package_name == package_name)

            cve_total = (await db.execute(cve_count_q)).scalar() or 0
            custom_total = (await db.execute(custom_count_q)).scalar() or 0
            total = int(cve_total + custom_total)

            # ---- fetch top K CVE matches
            cve_q = (
                select(
                    CveMatch.id,
                    CveMatch.created_at,
                    CveMatch.cve_id,
                    CveMatch.upstream_url,
                    CveMatch.recommended_fix,
                    CveMatch.summary,
                    CveMatch.severity,
                    CveMatch.is_handled,
                    CveMatch.osv_fixed_version,
                    CveMatch.handled_reason,
                    CveMatch.handled_notes,
                    TrackedRpm.package_name,
                    TrackedRpm.rpm_version,
                    TrackedRpm.rpm_release,
                )
                .join(TrackedRpm, CveMatch.tracked_rpm_id == TrackedRpm.id)
            )
            if release_id is not None:
                cve_q = cve_q.join(CveMatchRelease, CveMatchRelease.cve_match_id == CveMatch.id).where(
                    CveMatchRelease.release_id == release_id
                )
            if package_name:
                cve_q = cve_q.where(TrackedRpm.package_name == package_name)
            if sp_pred is not None:
                cve_q = cve_q.where(sp_pred)

            cve_q = cve_q.order_by(CveMatch.created_at.desc()).limit(k)
            cve_rows = (await db.execute(cve_q)).all()
            cve_ids = [int(r[0]) for r in cve_rows]

            # ---- fetch top K custom advisories
            custom_q = (
                select(
                    SecurityAdvisory.id,
                    SecurityAdvisory.created_at,
                    SecurityAdvisory.title,
                    SecurityAdvisory.cve_id,
                    SecurityAdvisory.upstream_url,
                    SecurityAdvisory.recommended_fix,
                    SecurityAdvisory.summary,
                    SecurityAdvisory.severity,
                    SecurityAdvisory.is_handled,
                    SecurityAdvisory.package_name,
                    SecurityAdvisory.package_version,
                )
                .select_from(SecurityAdvisory)
            )
            if release_id is not None:
                custom_q = custom_q.join(
                    SecurityAdvisoryRelease, SecurityAdvisoryRelease.advisory_id == SecurityAdvisory.id
                ).where(SecurityAdvisoryRelease.release_id == release_id)
            if package_name:
                custom_q = custom_q.where(SecurityAdvisory.package_name == package_name)
            custom_q = custom_q.order_by(SecurityAdvisory.created_at.desc()).limit(k)
            custom_rows = (await db.execute(custom_q)).all()
            custom_ids = [int(r[0]) for r in custom_rows]

            # ---- affected releases mapping for CVEs
            cve_rel_map: dict[int, set[int]] = {cid: set() for cid in cve_ids}
            cve_rel_ids: set[int] = set()
            if cve_ids:
                rel_rows = await db.execute(
                    select(CveMatchRelease.cve_match_id, CveMatchRelease.release_id).where(
                        CveMatchRelease.cve_match_id.in_(cve_ids)
                    )
                )
                for cid, rid in rel_rows.all():
                    cve_rel_map.setdefault(int(cid), set()).add(int(rid))
                    cve_rel_ids.add(int(rid))

            # ---- affected releases mapping for custom advisories
            custom_rel_map: dict[int, set[int]] = {aid: set() for aid in custom_ids}
            custom_rel_ids: set[int] = set()
            if custom_ids:
                rel_rows = await db.execute(
                    select(SecurityAdvisoryRelease.advisory_id, SecurityAdvisoryRelease.release_id).where(
                        SecurityAdvisoryRelease.advisory_id.in_(custom_ids)
                    )
                )
                for aid, rid in rel_rows.all():
                    custom_rel_map.setdefault(int(aid), set()).add(int(rid))
                    custom_rel_ids.add(int(rid))

            all_rel_ids = cve_rel_ids.union(custom_rel_ids)
            release_map: dict[int, str] = {}
            if all_rel_ids:
                release_rows = await db.execute(
                    select(Release.id, Release.releasename).where(Release.id.in_(all_rel_ids))
                )
                release_map = {int(rid): name for rid, name in release_rows.all()}

            # ---- build items lists with created_at for merge/slice
            items_with_sort: list[dict] = []

            for (
                cid,
                created_at,
                cve_id,
                upstream_url,
                recommended_fix,
                summary,
                severity,
                is_handled,
                osv_fixed_version,
                handled_reason,
                handled_notes,
                pkg_name,
                rpm_ver,
                rpm_rel,
            ) in cve_rows:
                affected = [
                    {"release_id": rid, "releasename": release_map.get(rid)}
                    for rid in sorted(cve_rel_map.get(int(cid), set()))
                ]
                items_with_sort.append(
                    {
                        "kind": "cve",
                        "id": int(cid),
                        "cve_id": cve_id,
                        "title": None,
                        "package_name": pkg_name,
                        "package_version": f"{rpm_ver}-{rpm_rel}" if rpm_ver and rpm_rel else None,
                        "upstream_url": upstream_url,
                        "recommended_fix": recommended_fix,
                        "summary": summary,
                        "severity": severity,
                        "affected_releases": affected,
                        "is_handled": bool(is_handled),
                        "osv_fixed_version": osv_fixed_version,
                        "handled_reason": handled_reason,
                        "handled_notes": handled_notes,
                        "_created_at": created_at or datetime.now(timezone.utc),
                    }
                )

            for (
                aid,
                created_at,
                title,
                cve_id,
                upstream_url,
                recommended_fix,
                summary,
                severity,
                is_handled,
                pkg_name,
                pkg_version,
            ) in custom_rows:
                affected = [
                    {"release_id": rid, "releasename": release_map.get(rid)}
                    for rid in sorted(custom_rel_map.get(int(aid), set()))
                ]
                items_with_sort.append(
                    {
                        "kind": "custom",
                        "id": int(aid),
                        "cve_id": cve_id,
                        "title": title,
                        "package_name": pkg_name,
                        "package_version": pkg_version,
                        "upstream_url": upstream_url,
                        "recommended_fix": recommended_fix,
                        "summary": summary,
                        "severity": severity,
                        "affected_releases": affected,
                        "is_handled": bool(is_handled),
                        "_created_at": created_at or datetime.now(timezone.utc),
                    }
                )

            items_with_sort.sort(key=lambda x: x.get("_created_at"), reverse=True)
            page_items = items_with_sort[offset : offset + limit]

            for it in page_items:
                it.pop("_created_at", None)

            return JSONResponse(content={"items": page_items, "total": total})

    @app.get("/api/security/releases")
    async def list_releases():
        from sqlalchemy import select

        async with async_session_maker() as db:
            res = await db.execute(
                select(Release).order_by(Release.releasename)
            )
            releases = res.scalars().all()
            return JSONResponse(
                content={
                    "items": [
                        {"id": r.id, "releasename": r.releasename, "default_channel": r.default_channel.value}
                        for r in releases
                    ]
                }
            )

    @app.get("/api/security/summary")
    async def security_summary(
        release_id: int | None = Query(None),
        package_name: str | None = Query(None),
        omit_subpackages: bool = Query(True),
    ):
        """Counts for the dashboard header row (respects the same filters as the tables)."""
        from sqlalchemy import func, select

        st = get_app_settings()
        sp_suf = parse_omit_subpackage_suffixes(str(st.cve_scan_omit_subpackage_suffixes or ""))
        sp_pred = tracked_rpm_not_subpackage_predicate(sp_suf) if omit_subpackages and sp_suf else None

        async with async_session_maker() as db:

            def cve_match_count_q(*, open_only: bool):
                q = (
                    select(func.count(func.distinct(CveMatch.id)))
                    .select_from(CveMatch)
                    .join(TrackedRpm, CveMatch.tracked_rpm_id == TrackedRpm.id)
                )
                if open_only:
                    q = q.where(CveMatch.is_handled.is_(False))
                if release_id is not None:
                    q = q.join(CveMatchRelease, CveMatchRelease.cve_match_id == CveMatch.id).where(
                        CveMatchRelease.release_id == release_id
                    )
                if package_name:
                    q = q.where(TrackedRpm.package_name == package_name)
                if sp_pred is not None:
                    q = q.where(sp_pred)
                return q

            open_cve = int((await db.execute(cve_match_count_q(open_only=True))).scalar() or 0)
            total_cve = int((await db.execute(cve_match_count_q(open_only=False))).scalar() or 0)

            custom_q = (
                select(func.count(func.distinct(SecurityAdvisory.id)))
                .select_from(SecurityAdvisory)
                .where(SecurityAdvisory.is_handled.is_(False))
            )
            if release_id is not None:
                custom_q = custom_q.join(
                    SecurityAdvisoryRelease, SecurityAdvisoryRelease.advisory_id == SecurityAdvisory.id
                ).where(SecurityAdvisoryRelease.release_id == release_id)
            if package_name:
                custom_q = custom_q.where(SecurityAdvisory.package_name == package_name)
            open_custom = int((await db.execute(custom_q)).scalar() or 0)

            monitored_q = select(func.count()).select_from(TrackedRpm)
            if package_name:
                monitored_q = monitored_q.where(TrackedRpm.package_name == package_name)
            if sp_pred is not None:
                monitored_q = monitored_q.where(sp_pred)
            monitored = int((await db.execute(monitored_q)).scalar() or 0)

        return JSONResponse(
            content={
                "open_cve_match_count": open_cve,
                "total_cve_match_count": total_cve,
                "open_custom_advisory_count": open_custom,
                "monitored_rpm_count": monitored,
            }
        )

    @app.get("/api/security/diagnostics")
    async def security_diagnostics(_admin=Depends(require_admin)):
        """
        Table row counts and R2 config flags so you can tell an empty UI from a wrong DATABASE_URL
        or a separate empty Postgres instance.
        """
        from sqlalchemy import func, not_, select
        from urllib.parse import urlparse

        s = get_app_settings()
        parsed = urlparse(str(s.database_url).replace("+asyncpg", ""))
        db_host = parsed.hostname or "?"
        db_name = (parsed.path or "").lstrip("/") or "?"

        r2_configured = bool(
            (s.r2_endpoint_url or "").strip()
            and (s.r2_bucket_name or "").strip()
            and (s.r2_access_key_id or "").strip()
            and (s.r2_secret_access_key or "").strip()
        )

        async with async_session_maker() as db:
            n_rel = int((await db.execute(select(func.count()).select_from(Release))).scalar() or 0)
            n_tr = int((await db.execute(select(func.count()).select_from(TrackedRpm))).scalar() or 0)
            n_cve = int((await db.execute(select(func.count()).select_from(CveMatch))).scalar() or 0)
            n_art_rpm = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(Artifact)
                        .where(
                            Artifact.kind == "rpm",
                            Artifact.filename.like("%.rpm"),
                            not_(Artifact.filename.like("%.src.rpm")),
                        )
                    )
                ).scalar()
                or 0
            )
            n_art_rpms_key = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(Artifact)
                        .where(
                            Artifact.kind == "rpm",
                            Artifact.filename.like("%.rpm"),
                            not_(Artifact.filename.like("%.src.rpm")),
                            Artifact.r2_key.like("%/RPMS/%"),
                        )
                    )
                ).scalar()
                or 0
            )
            n_art_builds_key = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(Artifact)
                        .where(
                            Artifact.kind == "rpm",
                            Artifact.filename.like("%.rpm"),
                            not_(Artifact.filename.like("%.src.rpm")),
                            Artifact.r2_key.like("%/builds/%"),
                        )
                    )
                ).scalar()
                or 0
            )

        content = {
            "database_host": db_host,
            "database_name": db_name,
            "release_row_count": n_rel,
            "tracked_rpm_row_count": n_tr,
            "cve_match_row_count": n_cve,
            "artifact_rpm_row_count": n_art_rpm,
            "artifact_rpm_r2_key_contains_rpms": n_art_rpms_key,
            "artifact_rpm_r2_key_contains_builds": n_art_builds_key,
            "r2_env_configured": r2_configured,
            "advisory_source": "manual_and_legacy_db_only",
            "cve_scan_interval_minutes": int(getattr(s, "cve_scan_interval_minutes", 30) or 30),
            "hint": "Watchdog does not call OSV, NVD, or R2 for CVEs. New advisories are custom entries you create. cve_match_row_count may be legacy rows from older runs. If tracked_rpm_row_count is 0 but you expect packages, confirm this process uses the same database as the build API.",
        }
        return JSONResponse(content=content)

    @app.get("/api/security/tracked-packages")
    async def list_tracked_packages(
        package_name: str | None = Query(None),
        omit_subpackages: bool = Query(True),
        limit: int = Query(200, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        """
        RPM name plus version-release from build artifacts (refreshed on the Watchdog schedule).
        CVE-style rows in the advisories list are only from the database (legacy or future imports), not live OSV/NVD.
        """
        from sqlalchemy import func, select

        st = get_app_settings()
        sp_suf = parse_omit_subpackage_suffixes(str(st.cve_scan_omit_subpackage_suffixes or ""))
        sp_pred = tracked_rpm_not_subpackage_predicate(sp_suf) if omit_subpackages and sp_suf else None

        cve_n = (
            select(func.count(CveMatch.id))
            .where(CveMatch.tracked_rpm_id == TrackedRpm.id)
            .scalar_subquery()
        )
        open_n = (
            select(func.count(CveMatch.id))
            .where(CveMatch.tracked_rpm_id == TrackedRpm.id, CveMatch.is_handled.is_(False))
            .scalar_subquery()
        )

        async with async_session_maker() as db:
            total_q = select(func.count()).select_from(TrackedRpm)
            if package_name:
                total_q = total_q.where(TrackedRpm.package_name == package_name)
            if sp_pred is not None:
                total_q = total_q.where(sp_pred)
            total = int((await db.execute(total_q)).scalar() or 0)

            q = select(
                TrackedRpm.id,
                TrackedRpm.package_name,
                TrackedRpm.rpm_version,
                TrackedRpm.rpm_release,
                TrackedRpm.last_checked_at,
                cve_n.label("cve_match_count"),
                open_n.label("open_cve_match_count"),
            )
            if package_name:
                q = q.where(TrackedRpm.package_name == package_name)
            if sp_pred is not None:
                q = q.where(sp_pred)
            q = q.order_by(TrackedRpm.package_name, TrackedRpm.rpm_version, TrackedRpm.rpm_release).offset(
                offset
            ).limit(limit)
            rows = (await db.execute(q)).all()

        items = [
            {
                "id": int(rid),
                "package_name": pn,
                "package_version": f"{rv}-{rr}" if rv and rr else None,
                "last_checked_at": _dt_to_iso(lat),
                "cve_match_count": int(cve_c or 0),
                "open_cve_match_count": int(open_c or 0),
            }
            for rid, pn, rv, rr, lat, cve_c, open_c in rows
        ]
        return JSONResponse(content={"items": items, "total": total})

    @app.post("/api/security/scan")
    async def trigger_scan(_account=Depends(require_admin)):
        # Fire-and-forget scan; return immediately.
        async def _run():
            try:
                lock = getattr(app.state, "scan_lock", None)
                if lock is None:
                    await scan_cves_once()
                    return
                async with lock:
                    logger.info("Watchdog inventory sync started reason=manual")
                    await scan_cves_once()
                    logger.info("Watchdog inventory sync finished reason=manual")
            except Exception:
                logger.exception("Manual inventory sync failed")

        asyncio.create_task(_run())
        return JSONResponse(content={"ok": True})

    @app.get("/api/security/advisories/cve/{cve_match_id}")
    async def get_cve_advisory(cve_match_id: int):
        from sqlalchemy import select

        async with async_session_maker() as db:
            q = (
                select(
                    CveMatch.id,
                    CveMatch.created_at,
                    CveMatch.cve_id,
                    CveMatch.upstream_url,
                    CveMatch.recommended_fix,
                    CveMatch.summary,
                    CveMatch.severity,
                    CveMatch.is_handled,
                    CveMatch.handled_at,
                    CveMatch.handled_by_account_id,
                    CveMatch.osv_fixed_version,
                    CveMatch.handled_reason,
                    CveMatch.handled_notes,
                    TrackedRpm.package_name,
                    TrackedRpm.rpm_version,
                    TrackedRpm.rpm_release,
                )
                .join(TrackedRpm, CveMatch.tracked_rpm_id == TrackedRpm.id)
                .where(CveMatch.id == cve_match_id)
            )
            row = (await db.execute(q)).one_or_none()
            if not row:
                return JSONResponse(content={"detail": "CVE advisory not found"}, status_code=404)

            (
                cid,
                created_at,
                cve_id,
                upstream_url,
                recommended_fix,
                summary,
                severity,
                is_handled,
                handled_at,
                handled_by_account_id,
                osv_fixed_version,
                handled_reason,
                handled_notes,
                pkg_name,
                rpm_ver,
                rpm_rel,
            ) = row

            rel_rows = await db.execute(
                select(CveMatchRelease.release_id).where(CveMatchRelease.cve_match_id == int(cve_match_id))
            )
            rel_ids = [int(r[0]) for r in rel_rows.all()]
            rel_map: dict[int, str] = {}
            if rel_ids:
                release_rows = await db.execute(
                    select(Release.id, Release.releasename).where(Release.id.in_(set(rel_ids)))
                )
                rel_map = {int(rid): name for rid, name in release_rows.all()}

            affected = [
                {"release_id": rid, "releasename": rel_map.get(rid)} for rid in sorted(set(rel_ids))
            ]

            return JSONResponse(
                content={
                    "kind": "cve",
                    "id": int(cid),
                    "cve_id": cve_id,
                    "title": None,
                    "package_name": pkg_name,
                    "package_version": f"{rpm_ver}-{rpm_rel}" if rpm_ver and rpm_rel else None,
                    "upstream_url": upstream_url,
                    "recommended_fix": recommended_fix,
                    "summary": summary,
                    "severity": severity,
                    "affected_releases": affected,
                    "is_handled": bool(is_handled),
                    "handled_at": _dt_to_iso(handled_at),
                    "handled_by_account_id": handled_by_account_id,
                    "osv_fixed_version": osv_fixed_version,
                    "handled_reason": handled_reason,
                    "handled_notes": handled_notes,
                }
            )

    @app.get("/api/security/advisories/custom/{advisory_id}")
    async def get_custom_advisory(advisory_id: int):
        from sqlalchemy import select

        async with async_session_maker() as db:
            q = select(
                SecurityAdvisory.id,
                SecurityAdvisory.created_at,
                SecurityAdvisory.title,
                SecurityAdvisory.cve_id,
                SecurityAdvisory.upstream_url,
                SecurityAdvisory.recommended_fix,
                SecurityAdvisory.summary,
                SecurityAdvisory.severity,
                SecurityAdvisory.is_handled,
                SecurityAdvisory.handled_at,
                SecurityAdvisory.handled_by_account_id,
                SecurityAdvisory.package_name,
                SecurityAdvisory.package_version,
            ).where(SecurityAdvisory.id == advisory_id)

            row = (await db.execute(q)).one_or_none()
            if not row:
                return JSONResponse(content={"detail": "Custom advisory not found"}, status_code=404)

            (
                aid,
                created_at,
                title,
                cve_id,
                upstream_url,
                recommended_fix,
                summary,
                severity,
                is_handled,
                handled_at,
                handled_by_account_id,
                pkg_name,
                pkg_version,
            ) = row

            rel_rows = await db.execute(
                select(SecurityAdvisoryRelease.release_id).where(
                    SecurityAdvisoryRelease.advisory_id == int(advisory_id)
                )
            )
            rel_ids = [int(r[0]) for r in rel_rows.all()]
            rel_map: dict[int, str] = {}
            if rel_ids:
                release_rows = await db.execute(
                    select(Release.id, Release.releasename).where(Release.id.in_(set(rel_ids)))
                )
                rel_map = {int(rid): name for rid, name in release_rows.all()}

            affected = [
                {"release_id": rid, "releasename": rel_map.get(rid)} for rid in sorted(set(rel_ids))
            ]

            return JSONResponse(
                content={
                    "kind": "custom",
                    "id": int(aid),
                    "cve_id": cve_id,
                    "title": title,
                    "package_name": pkg_name,
                    "package_version": pkg_version,
                    "upstream_url": upstream_url,
                    "recommended_fix": recommended_fix,
                    "summary": summary,
                    "severity": severity,
                    "affected_releases": affected,
                    "is_handled": bool(is_handled),
                    "handled_at": _dt_to_iso(handled_at),
                    "handled_by_account_id": handled_by_account_id,
                }
            )

    @app.post("/api/security/advisories/custom")
    async def create_custom_advisory(
        request: Request,
        payload: dict = Body(...),
        admin=Depends(require_admin),
    ):
        # Validate manually to avoid FastAPI/Pydantic ForwardRef issues
        # when this app defines models inside nested app factories.
        data = CustomAdvisoryCreate.model_validate(payload)
        from datetime import datetime, timezone
        from sqlalchemy import select

        if not data.affected_release_ids:
            return JSONResponse(content={"detail": "affected_release_ids required"}, status_code=400)

        async with async_session_maker() as db:
            releases_q = await db.execute(
                select(Release.id).where(Release.id.in_(set(map(int, data.affected_release_ids))))
            )
            release_ids_found = {int(r[0]) for r in releases_q.all()}
            expected_ids = {int(i) for i in data.affected_release_ids}
            if not expected_ids.issubset(release_ids_found):
                return JSONResponse(content={"detail": "One or more release IDs do not exist"}, status_code=400)

            adv = SecurityAdvisory(
                title=data.title,
                cve_id=data.cve_id,
                package_name=data.package_name,
                package_version=data.package_version,
                severity=data.severity,
                upstream_url=data.upstream_url,
                summary=data.summary,
                recommended_fix=data.recommended_fix,
                is_handled=False,
                created_by_account_id=admin.id,
            )
            db.add(adv)
            await db.flush()

            for rid in sorted(expected_ids):
                db.add(SecurityAdvisoryRelease(advisory_id=adv.id, release_id=int(rid)))

            await db.commit()

            # Send Discord notification for the new advisory.
            # Keep it best-effort; ignore failures.
            try:
                rel_name_q = await db.execute(
                    select(Release.id, Release.releasename).where(Release.id.in_(expected_ids))
                )
                rel_name_map = {int(rid): name for rid, name in rel_name_q.all()}
                releases_text = ", ".join([rel_name_map.get(rid) or str(rid) for rid in sorted(expected_ids)])

                fields = [
                    ("Type", "custom"),
                    ("Title", data.title),
                    ("CVE (optional)", data.cve_id or "-"),
                    ("Package", f"{data.package_name}-{data.package_version}"),
                    ("Upstream", data.upstream_url or "-"),
                    ("Fix", data.recommended_fix or "-"),
                    ("Severity", data.severity or "-"),
                    ("Affected Oreon releases", releases_text),
                ]
                await send_security_discord_embed(
                    title=f"security.advisory / {data.title}",
                    description=data.summary or "",
                    fields=fields,
                )
            except Exception:
                # Don't break the request for webhook failures.
                pass

            return JSONResponse(content={"ok": True, "id": adv.id})

    @app.post("/api/security/advisories/cve/{cve_match_id}/resolve")
    async def resolve_cve_advisory(
        cve_match_id: int,
        payload: CveResolveBody | None = Body(default=None),
        admin=Depends(require_admin),
    ):
        from datetime import datetime, timezone
        from sqlalchemy import select

        reason = (payload.reason or "").strip() if payload else ""
        notes = (payload.notes or "").strip() if payload else ""
        if reason and reason not in CVE_RESOLVE_REASONS:
            raise HTTPException(
                status_code=400,
                detail=f"reason must be one of: {', '.join(sorted(CVE_RESOLVE_REASONS))}",
            )
        reason_val = reason or None
        notes_val = notes or None

        async with async_session_maker() as db:
            q = select(CveMatch).where(CveMatch.id == cve_match_id)
            res = await db.execute(q)
            m = res.scalar_one_or_none()
            if not m:
                return JSONResponse(content={"detail": "CVE advisory not found"}, status_code=404)
            if m.is_handled:
                return JSONResponse(content={"ok": True, "already_resolved": True})
            m.is_handled = True
            m.handled_at = datetime.now(timezone.utc)
            m.handled_by_account_id = admin.id
            m.handled_reason = reason_val
            m.handled_notes = notes_val
            await db.commit()
            return JSONResponse(content={"ok": True})

    @app.post("/api/security/advisories/custom/{advisory_id}/resolve")
    async def resolve_custom_advisory(advisory_id: int, admin=Depends(require_admin)):
        from datetime import datetime, timezone
        from sqlalchemy import select

        async with async_session_maker() as db:
            q = select(SecurityAdvisory).where(SecurityAdvisory.id == advisory_id)
            res = await db.execute(q)
            adv = res.scalar_one_or_none()
            if not adv:
                return JSONResponse(content={"detail": "Custom advisory not found"}, status_code=404)
            if adv.is_handled:
                return JSONResponse(content={"ok": True, "already_resolved": True})
            adv.is_handled = True
            adv.handled_at = datetime.now(timezone.utc)
            adv.handled_by_account_id = admin.id
            await db.commit()
            return JSONResponse(content={"ok": True})

    return app


app = create_app()

