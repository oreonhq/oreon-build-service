from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field
from sqlalchemy.orm import selectinload
from oreon_build.db.session import async_session_maker
from oreon_build.core.auth import require_admin, require_account, require_maintainer_or_admin
from oreon_build.api.routers.auth import router as auth_router
from oreon_build.models import (
    CveMatch,
    CveMatchRelease,
    Release,
    SecurityAdvisory,
    SecurityAdvisoryRelease,
    TrackedRpm,
)
from oreon_build.config import get_settings as get_app_settings
from oreon_build.services.discord import DISCORD_COLOR_SUCCESS, send_security_discord_embed

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
    reason: str | None = Field(None, max_length=32)
    notes: str | None = Field(None, max_length=4000)

class CustomAdvisoryCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    cve_id: str | None = Field(None, max_length=64)
    severity: str | None = Field(None, max_length=32)
    upstream_url: str | None = Field(None, max_length=1024)
    summary: str | None = Field(None)
    recommended_fix: str | None = Field(None)
    affected_release_ids: list[int] = Field(..., min_length=1)

class CustomAdvisoryUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=256)
    cve_id: str | None = Field(None, max_length=64)
    severity: str | None = Field(None, max_length=32)
    upstream_url: str | None = Field(None, max_length=1024)
    summary: str | None = Field(None)
    recommended_fix: str | None = Field(None)
    affected_release_ids: list[int] | None = Field(None, min_length=1)

def _dt_to_iso(dt) -> str | None:
    if not dt:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)

_US_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

def _dt_utc(dt):
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        from datetime import timezone as _tz

        return dt.replace(tzinfo=_tz.utc)
    return dt

def _fmt_us_date(dt) -> str:
    d = _dt_utc(dt)
    if d is None:
        return "-"
    return f"{_US_MONTHS[d.month - 1]} {d.day}, {d.year}"

def _errata_year_for_id(dt) -> int:
    d = _dt_utc(dt)
    if d is None:
        from datetime import datetime as _dt, timezone as _tz

        return _dt.now(_tz.utc).year
    return d.year

def _orsa_errata_id(*, kind: str, numeric_id: int, dt_for_year) -> str:
    y = _errata_year_for_id(dt_for_year)
    return f"ORSA-{y}:{numeric_id}"

def _capitalize_severity(sev: str | None) -> str:
    s = (sev or "").strip().lower()
    if not s:
        return "Moderate"
    return s[:1].upper() + s[1:] if len(s) > 1 else s.upper()

def _bulletin_title(
    *,
    kind: str,
    severity: str | None,
    cve_id: str | None,
    title: str | None,
) -> str:
    sev = _capitalize_severity(severity)
    cve = (cve_id or "").strip()
    if kind == "custom" and (title or "").strip():
        t = (title or "").strip()
        if cve:
            return f"{sev}: {t} ({cve})"[:512]
        return f"{sev}: {t}"[:512]
    if kind == "cve" and cve:
        return f"{sev}: {cve} security update"
    return f"{sev}: security update"

def _platform_display(affected: list[dict]) -> str:
    names: list[str] = []
    for row in affected or []:
        rn = (row.get("releasename") or "").strip()
        if rn and rn not in names:
            names.append(rn)
    if not names:
        return "-"
    return ", ".join(names)

def _custom_advisory_detail(
    adv: SecurityAdvisory,
    affected: list[dict],
) -> dict:
    return {
        "kind": "custom",
        "id": int(adv.id),
        "cve_id": adv.cve_id,
        "title": adv.title,
        "upstream_url": adv.upstream_url,
        "recommended_fix": adv.recommended_fix,
        "summary": adv.summary,
        "severity": adv.severity,
        "affected_releases": affected,
        "is_handled": bool(adv.is_handled),
        "handled_at": _dt_to_iso(adv.handled_at),
        "handled_by_account_id": adv.handled_by_account_id,
        "_created_at": adv.created_at,
        "_handled_at": adv.handled_at,
    }

def _enrich_errata_row(d: dict) -> None:
    kind = str(d.get("kind") or "")
    numeric_id = int(d.get("id") or 0)
    created = d.get("_created_at")
    handled_at = d.get("_handled_at")
    is_handled = bool(d.get("is_handled"))
    sort_ts = handled_at if is_handled and handled_at else created
    d["errata_id"] = _orsa_errata_id(kind=kind, numeric_id=numeric_id, dt_for_year=sort_ts or created)
    d["bulletin_title"] = _bulletin_title(
        kind=kind,
        severity=d.get("severity"),
        cve_id=d.get("cve_id"),
        title=d.get("title"),
    )
    d["advisory_status"] = "released" if is_handled else "unresolved"
    d["status_label"] = "Resolved" if is_handled else "Unresolved"
    d["status_sub_label"] = "" if is_handled else "not resolved"
    upd = handled_at if is_handled and handled_at else created
    d["updated_at"] = _dt_to_iso(upd)
    d["updated_at_display"] = _fmt_us_date(upd)
    d["platform_display"] = _platform_display(d.get("affected_releases") or [])

def _json_advisory_detail(d: dict) -> dict:
    out = dict(d)
    _enrich_errata_row(out)
    out.pop("_created_at", None)
    out.pop("_handled_at", None)
    out.pop("_sort_ts", None)
    if "_created_at" in d:
        out["created_at"] = _dt_to_iso(d.get("_created_at"))
    return out

async def _discord_notify_advisory_resolved(
    *,
    kind: str,
    row: dict,
    admin_username: str,
    extra_fields: list[tuple[str, str]] | None = None,
) -> None:
    d = dict(row)
    _enrich_errata_row(d)
    d.pop("_created_at", None)
    d.pop("_handled_at", None)
    d.pop("_sort_ts", None)
    fields: list[tuple[str, str]] = [
        ("Type", "CVE match" if kind == "cve" else "Advisory"),
        ("ORSA id", str(d.get("errata_id") or "-")),
        ("Advisory title", str(d.get("bulletin_title") or "-")),
        ("Platform", str(d.get("platform_display") or "-")),
    ]
    if extra_fields:
        fields.extend(extra_fields)
    await send_security_discord_embed(
        title="security.advisory / resolved",
        description=f"Marked resolved by {admin_username}",
        fields=fields,
        color=DISCORD_COLOR_SUCCESS,
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_watchdog_logging()
    yield

def create_app() -> FastAPI:
    app = FastAPI(title="Oreon Watchdog", version="1.0.0", lifespan=lifespan)

    web_dir = Path(__file__).resolve().parent.parent.parent.parent / "web"
    if not web_dir.exists():
        raise RuntimeError(f"web directory not found: {web_dir}")

    app.mount("/static", StaticFiles(directory=str(web_dir / "static")), name="static")

    app.include_router(auth_router, prefix="/api")

    @app.get("/")
    async def _index():
        return FileResponse(web_dir / "watchdog.html")

    @app.get("/advisories/cve/{cve_match_id}")
    async def _advisory_cve(cve_match_id: int):
        return FileResponse(web_dir / "advisory-cve.html")

    @app.get("/advisories/custom/{advisory_id}")
    async def _advisory_custom_legacy(advisory_id: int):
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url=f"/advisories/{advisory_id}", status_code=307)

    @app.get("/advisories/{advisory_id}")
    async def _advisory_detail(advisory_id: int):
        return FileResponse(web_dir / "advisory-custom.html")

    @app.get("/api/security/advisories")
    async def list_advisories(
        release_id: int | None = Query(None),
        limit: int = Query(25, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        from datetime import datetime, timezone
        from sqlalchemy import func, select

        async with async_session_maker() as db:
            count_q = select(func.count(func.distinct(SecurityAdvisory.id))).select_from(SecurityAdvisory)
            if release_id is not None:
                count_q = count_q.join(
                    SecurityAdvisoryRelease, SecurityAdvisoryRelease.advisory_id == SecurityAdvisory.id
                ).where(SecurityAdvisoryRelease.release_id == release_id)
            total = int((await db.execute(count_q)).scalar() or 0)

            q = (
                select(
                    SecurityAdvisory.id,
                    SecurityAdvisory.created_at,
                    SecurityAdvisory.handled_at,
                    SecurityAdvisory.title,
                    SecurityAdvisory.cve_id,
                    SecurityAdvisory.upstream_url,
                    SecurityAdvisory.recommended_fix,
                    SecurityAdvisory.summary,
                    SecurityAdvisory.severity,
                    SecurityAdvisory.is_handled,
                )
                .select_from(SecurityAdvisory)
            )
            if release_id is not None:
                q = q.join(
                    SecurityAdvisoryRelease, SecurityAdvisoryRelease.advisory_id == SecurityAdvisory.id
                ).where(SecurityAdvisoryRelease.release_id == release_id)
            q = q.order_by(SecurityAdvisory.created_at.desc()).offset(offset).limit(limit)
            rows = (await db.execute(q)).all()
            adv_ids = [int(r[0]) for r in rows]

            rel_map: dict[int, set[int]] = {aid: set() for aid in adv_ids}
            all_rel_ids: set[int] = set()
            if adv_ids:
                rel_rows = await db.execute(
                    select(SecurityAdvisoryRelease.advisory_id, SecurityAdvisoryRelease.release_id).where(
                        SecurityAdvisoryRelease.advisory_id.in_(adv_ids)
                    )
                )
                for aid, rid in rel_rows.all():
                    rel_map.setdefault(int(aid), set()).add(int(rid))
                    all_rel_ids.add(int(rid))

            release_names: dict[int, str] = {}
            if all_rel_ids:
                release_rows = await db.execute(
                    select(Release.id, Release.releasename).where(Release.id.in_(all_rel_ids))
                )
                release_names = {int(rid): name for rid, name in release_rows.all()}

            items: list[dict] = []
            for (
                aid,
                created_at,
                handled_at,
                title,
                cve_id,
                upstream_url,
                recommended_fix,
                summary,
                severity,
                is_handled,
            ) in rows:
                affected = [
                    {"release_id": rid, "releasename": release_names.get(rid)}
                    for rid in sorted(rel_map.get(int(aid), set()))
                ]
                ca = created_at or datetime.now(timezone.utc)
                ha = handled_at
                item = {
                    "kind": "custom",
                    "id": int(aid),
                    "cve_id": cve_id,
                    "title": title,
                    "upstream_url": upstream_url,
                    "recommended_fix": recommended_fix,
                    "summary": summary,
                    "severity": severity,
                    "affected_releases": affected,
                    "is_handled": bool(is_handled),
                    "_created_at": ca,
                    "_handled_at": ha,
                }
                _enrich_errata_row(item)
                item.pop("_created_at", None)
                item.pop("_handled_at", None)
                items.append(item)

            return JSONResponse(content={"items": items, "total": total})

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
    ):
        from sqlalchemy import func, select

        async with async_session_maker() as db:
            open_q = (
                select(func.count(func.distinct(SecurityAdvisory.id)))
                .select_from(SecurityAdvisory)
                .where(SecurityAdvisory.is_handled.is_(False))
            )
            total_q = select(func.count(func.distinct(SecurityAdvisory.id))).select_from(SecurityAdvisory)
            if release_id is not None:
                open_q = open_q.join(
                    SecurityAdvisoryRelease, SecurityAdvisoryRelease.advisory_id == SecurityAdvisory.id
                ).where(SecurityAdvisoryRelease.release_id == release_id)
                total_q = total_q.join(
                    SecurityAdvisoryRelease, SecurityAdvisoryRelease.advisory_id == SecurityAdvisory.id
                ).where(SecurityAdvisoryRelease.release_id == release_id)
            open_custom = int((await db.execute(open_q)).scalar() or 0)
            total_custom = int((await db.execute(total_q)).scalar() or 0)

        return JSONResponse(
            content={
                "open_custom_advisory_count": open_custom,
                "total_custom_advisory_count": total_custom,
            }
        )

    @app.get("/api/security/diagnostics")
    async def security_diagnostics(_admin=Depends(require_admin)):
        from sqlalchemy import func, select
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
            n_adv = int((await db.execute(select(func.count()).select_from(SecurityAdvisory))).scalar() or 0)

        content = {
            "database_host": db_host,
            "database_name": db_name,
            "release_row_count": n_rel,
            "security_advisory_row_count": n_adv,
            "r2_env_configured": r2_configured,
            "advisory_source": "manual_only",
        }
        return JSONResponse(content=content)

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

            detail = {
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
                "_created_at": created_at,
                "_handled_at": handled_at,
            }
            return JSONResponse(content=_json_advisory_detail(detail))

    @app.get("/api/security/advisories/custom/{advisory_id}")
    async def get_custom_advisory(advisory_id: int):
        from sqlalchemy import select

        async with async_session_maker() as db:
            res = await db.execute(
                select(SecurityAdvisory).where(SecurityAdvisory.id == advisory_id)
            )
            adv = res.scalar_one_or_none()
            if not adv:
                return JSONResponse(content={"detail": "Advisory not found"}, status_code=404)

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
            return JSONResponse(content=_json_advisory_detail(_custom_advisory_detail(adv, affected)))

    @app.patch("/api/security/advisories/custom/{advisory_id}")
    async def update_custom_advisory(
        advisory_id: int,
        payload: dict = Body(...),
        _actor=Depends(require_maintainer_or_admin),
    ):
        from sqlalchemy import delete, select

        data = CustomAdvisoryUpdate.model_validate(payload)
        if not data.model_fields_set:
            return JSONResponse(content={"detail": "No fields to update"}, status_code=400)

        async with async_session_maker() as db:
            res = await db.execute(select(SecurityAdvisory).where(SecurityAdvisory.id == advisory_id))
            adv = res.scalar_one_or_none()
            if not adv:
                return JSONResponse(content={"detail": "Advisory not found"}, status_code=404)

            if "title" in data.model_fields_set:
                adv.title = data.title
            if "cve_id" in data.model_fields_set:
                adv.cve_id = data.cve_id
            if "severity" in data.model_fields_set:
                adv.severity = data.severity
            if "upstream_url" in data.model_fields_set:
                adv.upstream_url = data.upstream_url
            if "summary" in data.model_fields_set:
                adv.summary = data.summary
            if "recommended_fix" in data.model_fields_set:
                fix = (data.recommended_fix or "").strip()
                if not fix:
                    return JSONResponse(
                        content={"detail": "recommended_fix cannot be empty"}, status_code=400
                    )
                adv.recommended_fix = fix

            if "affected_release_ids" in data.model_fields_set:
                release_ids = data.affected_release_ids or []
                expected_ids = {int(i) for i in release_ids}
                if not expected_ids:
                    return JSONResponse(
                        content={"detail": "affected_release_ids required"}, status_code=400
                    )
                releases_q = await db.execute(
                    select(Release.id).where(Release.id.in_(expected_ids))
                )
                release_ids_found = {int(r[0]) for r in releases_q.all()}
                if not expected_ids.issubset(release_ids_found):
                    return JSONResponse(
                        content={"detail": "One or more release IDs do not exist"}, status_code=400
                    )
                await db.execute(
                    delete(SecurityAdvisoryRelease).where(
                        SecurityAdvisoryRelease.advisory_id == advisory_id
                    )
                )
                for rid in sorted(expected_ids):
                    db.add(SecurityAdvisoryRelease(advisory_id=adv.id, release_id=int(rid)))

            await db.commit()
            await db.refresh(adv)

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
            return JSONResponse(content=_json_advisory_detail(_custom_advisory_detail(adv, affected)))

    @app.post("/api/security/advisories/custom")
    async def create_custom_advisory(
        request: Request,
        payload: dict = Body(...),
        actor=Depends(require_maintainer_or_admin),
    ):
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
                severity=data.severity,
                upstream_url=data.upstream_url,
                summary=data.summary,
                recommended_fix=data.recommended_fix,
                is_handled=False,
                created_by_account_id=actor.id,
            )
            db.add(adv)
            await db.flush()

            for rid in sorted(expected_ids):
                db.add(SecurityAdvisoryRelease(advisory_id=adv.id, release_id=int(rid)))

            await db.commit()

            try:
                rel_name_q = await db.execute(
                    select(Release.id, Release.releasename).where(Release.id.in_(expected_ids))
                )
                rel_name_map = {int(rid): name for rid, name in rel_name_q.all()}
                releases_text = ", ".join([rel_name_map.get(rid) or str(rid) for rid in sorted(expected_ids)])

                fields = [
                    ("Type", "Advisory"),
                    ("Title", data.title),
                    ("CVE (optional)", data.cve_id or "-"),
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
                pass

            return JSONResponse(content={"ok": True, "id": adv.id})

    @app.post("/api/security/advisories/cve/{cve_match_id}/resolve")
    async def resolve_cve_advisory(
        cve_match_id: int,
        payload: CveResolveBody | None = Body(default=None),
        actor=Depends(require_maintainer_or_admin),
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
            q = (
                select(CveMatch)
                .where(CveMatch.id == cve_match_id)
                .options(selectinload(CveMatch.tracked_rpm))
            )
            res = await db.execute(q)
            m = res.scalar_one_or_none()
            if not m:
                return JSONResponse(content={"detail": "CVE advisory not found"}, status_code=404)
            if m.is_handled:
                return JSONResponse(content={"ok": True, "already_resolved": True})
            rel_rows = await db.execute(
                select(Release.releasename)
                .join(CveMatchRelease, CveMatchRelease.release_id == Release.id)
                .where(CveMatchRelease.cve_match_id == cve_match_id)
            )
            affected = [
                {"releasename": (name or "").strip()}
                for (name,) in rel_rows.all()
                if name and str(name).strip()
            ]
            rpm = m.tracked_rpm
            pkg_ver = f"{rpm.rpm_version}-{rpm.rpm_release}" if rpm else None
            handled_at = datetime.now(timezone.utc)
            m.is_handled = True
            m.handled_at = handled_at
            m.handled_by_account_id = actor.id
            m.handled_reason = reason_val
            m.handled_notes = notes_val
            await db.commit()
            try:
                await _discord_notify_advisory_resolved(
                    kind="cve",
                    row={
                        "kind": "cve",
                        "id": int(m.id),
                        "cve_id": m.cve_id,
                        "title": None,
                        "package_name": rpm.package_name if rpm else None,
                        "package_version": pkg_ver,
                        "severity": m.severity,
                        "affected_releases": affected,
                        "is_handled": True,
                        "_created_at": m.created_at,
                        "_handled_at": handled_at,
                    },
                    admin_username=actor.username,
                    extra_fields=[
                        ("CVE", m.cve_id or "-"),
                        ("Reason", reason_val or "-"),
                        ("Notes", notes_val or "-"),
                    ],
                )
            except Exception:
                pass
            return JSONResponse(content={"ok": True})

    @app.post("/api/security/advisories/custom/{advisory_id}/resolve")
    async def resolve_custom_advisory(
        advisory_id: int, actor=Depends(require_maintainer_or_admin)
    ):
        from datetime import datetime, timezone
        from sqlalchemy import select

        async with async_session_maker() as db:
            q = select(SecurityAdvisory).where(SecurityAdvisory.id == advisory_id)
            res = await db.execute(q)
            adv = res.scalar_one_or_none()
            if not adv:
                return JSONResponse(content={"detail": "Advisory not found"}, status_code=404)
            if adv.is_handled:
                return JSONResponse(content={"ok": True, "already_resolved": True})
            rel_rows = await db.execute(
                select(Release.releasename)
                .join(SecurityAdvisoryRelease, SecurityAdvisoryRelease.release_id == Release.id)
                .where(SecurityAdvisoryRelease.advisory_id == advisory_id)
            )
            affected = [
                {"releasename": (name or "").strip()}
                for (name,) in rel_rows.all()
                if name and str(name).strip()
            ]
            handled_at = datetime.now(timezone.utc)
            adv.is_handled = True
            adv.handled_at = handled_at
            adv.handled_by_account_id = actor.id
            await db.commit()
            try:
                await _discord_notify_advisory_resolved(
                    kind="custom",
                    row={
                        "kind": "custom",
                        "id": int(adv.id),
                        "cve_id": adv.cve_id,
                        "title": adv.title,
                        "package_name": adv.package_name,
                        "package_version": adv.package_version,
                        "severity": adv.severity,
                        "affected_releases": affected,
                        "is_handled": True,
                        "_created_at": adv.created_at,
                        "_handled_at": handled_at,
                    },
                    admin_username=actor.username,
                    extra_fields=[("CVE (optional)", adv.cve_id or "-")],
                )
            except Exception:
                pass
            return JSONResponse(content={"ok": True})

    return app

app = create_app()

