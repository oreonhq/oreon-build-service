# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging
import os
import random
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from oreon_build.config import Settings, get_settings
from oreon_build.db import get_db, init_db
from oreon_build.models import Account, Role, RoleName
from oreon_build.core.security import hash_password, verify_password

logger = logging.getLogger(__name__)

_github_catchup_misconfig_logged = False

def _too_many_connections(exc: BaseException) -> bool:
    if isinstance(exc, asyncpg.TooManyConnectionsError):
        return True
    if isinstance(exc, OperationalError):
        c = exc.__cause__
        return isinstance(c, asyncpg.TooManyConnectionsError)
    return False

def _configure_oreon_logging(settings: Settings) -> None:
    name = (settings.log_level or "INFO").upper()
    level = getattr(logging, name, logging.INFO)
    root = logging.getLogger("oreon_build")
    root.setLevel(level)
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        root.addHandler(h)
    root.propagate = False
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

from .routers import (
    auth,
    accounts,
    releases,
    packages,
    builds,
    workers,
    mock_envs,
    promotions,
    repos,
    schedules,
    logs,
    audit,
    search as search_router,
    github_webhook,
    side_tags,
    iso_builds,
)
from .worker_api import router as worker_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_oreon_logging(settings)
    logger.info(
        "API startup db_pool null_pool=%s pool_size=%s max_overflow=%s pool_timeout=%s",
        settings.database_use_null_pool,
        settings.database_pool_size,
        settings.database_max_overflow,
        settings.database_pool_timeout,
    )
    if settings.database_init_schema_on_startup:
        try:
            await init_db()
        except Exception as e:
            if settings.database_startup_bootstrap_strict:
                raise
            if _too_many_connections(e):
                logger.warning(
                    "Skipping init_db on startup due to database saturation (TooManyConnections). "
                    "Set DATABASE_STARTUP_BOOTSTRAP_STRICT=true to fail startup instead."
                )
            else:
                logger.exception("Skipping init_db on startup due to bootstrap error")
    logger.info(
        "API startup log_level=%s github_catchup_s=%s github_token=%s",
        settings.log_level,
        settings.github_catchup_interval_seconds,
        bool(settings.github_token),
    )
    from oreon_build.db.session import async_session_maker
    try:
        async with async_session_maker() as db:
            try:
                result = await db.execute(select(Role))
                roles = list(result.scalars().all())
                if not roles:
                    for r in [RoleName.ADMIN, RoleName.MAINTAINER]:
                        role = Role(name=r, description=r.value)
                        db.add(role)
                    await db.flush()
                result = await db.execute(select(Role).where(Role.name == RoleName.ADMIN))
                admin_role = result.scalar_one_or_none()
                if admin_role:
                    result = await db.execute(select(Account).where(Account.username == settings.admin_username))
                    admin = result.scalar_one_or_none()
                    if not admin:
                        admin = Account(
                            username=settings.admin_username,
                            password_hash=hash_password(settings.admin_password),
                            role_id=admin_role.id,
                            is_active=True,
                        )
                        db.add(admin)
                    else:
                        try:
                            if not verify_password(settings.admin_password, admin.password_hash):
                                admin.password_hash = hash_password(settings.admin_password)
                                db.add(admin)
                        except Exception:
                            logger.exception("Failed verifying/updating default admin password")
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    except Exception as e:
        if settings.database_startup_bootstrap_strict:
            raise
        if _too_many_connections(e):
            logger.warning(
                "Skipping role/admin startup bootstrap due to database saturation (TooManyConnections). "
                "Service will continue and recover once DB pressure drops."
            )
        else:
            logger.exception("Skipping role/admin startup bootstrap due to startup DB error")

    sweep_task: asyncio.Task[None] | None = None
    publish_task: asyncio.Task[None] | None = None

    async def _stale_build_sweep_loop() -> None:
        from oreon_build.db.session import async_session_maker
        from oreon_build.services.stale_builds import (
            reconcile_queue_inconsistencies,
            sweep_stale_pending_builds,
            sweep_stale_running_builds,
        )
        from oreon_build.services.stale_publish_tasks import reclaim_publish_tasks_stuck_running

        while True:
            try:
                s = get_settings()
                interval = max(60, s.stale_build_sweep_interval_seconds)
                async with async_session_maker() as db:
                    try:
                        await reconcile_queue_inconsistencies(db)
                        await reclaim_publish_tasks_stuck_running(db, s)
                        if s.build_stale_running_minutes > 0 or s.build_stale_worker_heartbeat_minutes > 0:
                            await sweep_stale_running_builds(db, s)
                        if s.build_stale_pending_minutes > 0:
                            await sweep_stale_pending_builds(db, s)
                        await db.commit()
                    except Exception:
                        await db.rollback()
                        raise
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Stale build sweep failed")
                try:
                    await asyncio.sleep(max(60, get_settings().stale_build_sweep_interval_seconds))
                except asyncio.CancelledError:
                    break

    sweep_task = asyncio.create_task(_stale_build_sweep_loop(), name="stale-build-sweep")

    async def _github_push_catchup_loop() -> None:
        global _github_catchup_misconfig_logged
        from oreon_build.db.session import async_session_maker
        from oreon_build.services.github_push_catchup import run_github_push_catchup

        first = True
        while True:
            try:
                s = get_settings()
                if (
                    s.github_catchup_interval_seconds <= 0
                    or not s.github_token
                    or not s.github_push_auto_build_enabled
                ):
                    if not _github_catchup_misconfig_logged:
                        _github_catchup_misconfig_logged = True
                        reasons: list[str] = []
                        if not s.github_token:
                            reasons.append(
                                "GITHUB_TOKEN is unset (use a GitHub PAT for api.github.com compare; "
                                "GITHUB_WEBHOOK_SECRET is only for verifying inbound webhooks)"
                            )
                        if s.github_catchup_interval_seconds <= 0:
                            reasons.append("GITHUB_CATCHUP_INTERVAL_SECONDS is 0")
                        if not s.github_push_auto_build_enabled:
                            reasons.append(
                                "GITHUB_PUSH_AUTO_BUILD_ENABLED is false (set true to enqueue distgit builds from webhooks and catch-up)"
                            )
                        logger.warning(
                            "GitHub push catch-up disabled: %s",
                            " ".join(reasons),
                        )
                    await asyncio.sleep(3600)
                    continue
                if not first:
                    await asyncio.sleep(max(60, s.github_catchup_interval_seconds))
                first = False
                async with async_session_maker() as db:
                    try:
                        result = await run_github_push_catchup(db)
                        await db.commit()
                        logger.info("GitHub catch-up loop result %s", result)
                    except Exception:
                        await db.rollback()
                        raise
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("GitHub push catch-up failed")
                try:
                    await asyncio.sleep(max(60, get_settings().github_catchup_interval_seconds or 600))
                except asyncio.CancelledError:
                    break

    catchup_task = asyncio.create_task(_github_push_catchup_loop(), name="github-push-catchup")

    async def _publish_tasks_loop() -> None:
        from oreon_build.db.session import async_session_maker
        from oreon_build.models import PublishTask
        from oreon_build.services.repo_publish import run_repo_publish_tasks_batch

        while True:
            try:
                raw_idle = (os.environ.get("OREON_PUBLISH_TASK_IDLE_POLL_SEC") or "15").strip()
                try:
                    idle_interval_s = float(raw_idle)
                except ValueError:
                    idle_interval_s = 15.0
                idle_interval_s = max(1.0, min(idle_interval_s, 60.0))
                async with async_session_maker() as db:
                    q = (
                        select(PublishTask)
                        .where(PublishTask.status == "pending")
                        .order_by(PublishTask.created_at.asc())
                        .with_for_update(of=PublishTask, skip_locked=True)
                    )
                    result = await db.execute(q)
                    tasks = list(result.scalars().all())
                    if tasks:
                        await db.commit()

                if not tasks:
                    await asyncio.sleep(random.uniform(idle_interval_s * 0.85, idle_interval_s * 1.15))
                    continue

                try:
                    await run_repo_publish_tasks_batch(tasks)
                except Exception:
                    logger.exception(
                        "Publish batch failed tasks=%s",
                        [(t.id, t.attempt_id) for t in tasks],
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Publish tasks loop error")
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    break

    publish_task = asyncio.create_task(_publish_tasks_loop(), name="publish-tasks")

    async def _mass_rebuild_loop() -> None:
        from oreon_build.db.session import async_session_maker
        from oreon_build.services.mass_rebuild import run_mass_rebuild_pass

        while True:
            try:
                s = get_settings()
                if not getattr(s, "mass_rebuild_enabled", False):
                    await asyncio.sleep(30)
                    continue
                raw_i = int(getattr(s, "mass_rebuild_interval_seconds", 300) or 300)
                raw_i = max(60, min(raw_i, 86400))
                async with async_session_maker() as db:
                    await run_mass_rebuild_pass(db)
                    await db.commit()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Mass rebuild pass error")
                try:
                    await asyncio.sleep(120)
                except asyncio.CancelledError:
                    break
                continue
            try:
                s2 = get_settings()
                raw_i = int(getattr(s2, "mass_rebuild_interval_seconds", 300) or 300)
                await asyncio.sleep(max(60, min(raw_i, 86400)))
            except asyncio.CancelledError:
                break

    mass_rebuild_task = asyncio.create_task(_mass_rebuild_loop(), name="mass-rebuild")

    async def _rpmbuild_settle_loop() -> None:
        from oreon_build.db.session import async_session_maker
        from oreon_build.services.rpmbuild_settle import advance_rpmbuild_plans_once

        while True:
            try:
                s = get_settings()
                if not s.rpmbuild_layers_enabled:
                    await asyncio.sleep(30)
                    continue
                async with async_session_maker() as db:
                    await advance_rpmbuild_plans_once(db)
                    await db.commit()
                await asyncio.sleep(max(5, int(s.rpmbuild_settle_poll_seconds)))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("rpmbuild settle loop error")
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    break

    rpmbuild_settle_task = asyncio.create_task(_rpmbuild_settle_loop(), name="rpmbuild-settle")

    async def _rpm_scan_loop() -> None:
        from oreon_build.db.session import async_session_maker
        from oreon_build.services.rpm_scan import retry_pending_scans

        while True:
            try:
                async with async_session_maker() as db:
                    await retry_pending_scans(db)
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("rpm scan loop error")
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    break

    rpm_scan_task = asyncio.create_task(_rpm_scan_loop(), name="rpm-scan")

    async def _burst_vm_watcher_loop() -> None:
        from oreon_build.db.session import async_session_maker
        from oreon_build.models import BuildJob, BuildStatus, Package, Worker
        from oreon_build.services.gcp_compute import start_instance
        from sqlalchemy import select, func

        while True:
            try:
                async with async_session_maker() as db:
                    burst_res = await db.execute(
                        select(Worker).where(Worker.is_burst_worker == True)  # noqa: E712
                    )
                    burst_workers = list(burst_res.scalars().all())
                    for bw in burst_workers:
                        whitelist_lower = {n.lower() for n in (bw.burst_whitelist or []) if n}
                        if not whitelist_lower:
                            continue
                        pending = await db.scalar(
                            select(func.count(BuildJob.id))
                            .join(Package, BuildJob.package_id == Package.id)
                            .where(
                                BuildJob.status.in_([BuildStatus.PENDING, BuildStatus.QUEUED]),
                                func.lower(Package.name).in_(whitelist_lower),
                            )
                        )
                        if (pending or 0) == 0:
                            continue
                        from datetime import datetime, timezone
                        now = datetime.now(timezone.utc)
                        hb = bw.last_heartbeat
                        if hb and hb.tzinfo is None:
                            hb = hb.replace(tzinfo=timezone.utc)
                        offline = hb is None or (now - hb).total_seconds() > 60
                        if offline:
                            instance = (bw.gcp_instance_name or "").strip()
                            if not instance:
                                logger.warning(
                                    "burst-vm: worker %s has pending jobs but no gcp_instance_name set, skipping VM start",
                                    bw.name,
                                )
                                continue
                            logger.info(
                                "burst-vm: worker %s has %d pending burst jobs but is offline, starting VM %s",
                                bw.name, pending, instance,
                            )
                            asyncio.create_task(
                                start_instance(instance, zone=bw.gcp_zone or None),
                                name=f"burst-vm-start-{bw.name}",
                            )
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("burst vm watcher loop error")
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    break

    burst_vm_task = asyncio.create_task(_burst_vm_watcher_loop(), name="burst-vm-watcher")

    async def _side_tag_inactivity_loop() -> None:
        from oreon_build.db.session import async_session_maker
        from oreon_build.services.side_tags import scrap_inactive_side_tags

        while True:
            try:
                await asyncio.sleep(3600)
                async with async_session_maker() as db:
                    try:
                        scrapped = await scrap_inactive_side_tags(db)
                        await db.commit()
                        if scrapped:
                            logger.info("auto-scrapped %d inactive side tags", len(scrapped))
                    except Exception:
                        await db.rollback()
                        raise
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("side tag inactivity scrap failed")
                try:
                    await asyncio.sleep(600)
                except asyncio.CancelledError:
                    break

    side_tag_idle_task = asyncio.create_task(
        _side_tag_inactivity_loop(), name="side-tag-inactivity"
    )

    yield

    for t, label in [
        (sweep_task, "stale-build-sweep"),
        (catchup_task, "github-push-catchup"),
        (publish_task, "publish-tasks"),
        (mass_rebuild_task, "mass-rebuild"),
        (rpmbuild_settle_task, "rpmbuild-settle"),
        (rpm_scan_task, "rpm-scan"),
        (burst_vm_task, "burst-vm-watcher"),
        (side_tag_idle_task, "side-tag-inactivity"),
    ]:
        if t is not None:
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                logger.exception("Error stopping background task %s", label)

def _cors_origins(settings: Settings) -> list[str]:
    origins = [
        "https://build.oreonhq.com",
        "https://watchdog.oreonhq.com",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:8001",
        "http://localhost:8001",
    ]
    wd = (settings.watchdog_url or "").rstrip("/")
    if wd and wd not in origins:
        origins.append(wd)
    for part in (settings.cors_origins or "").split(","):
        item = part.strip().rstrip("/")
        if item and item not in origins:
            origins.append(item)
    return origins


def create_app() -> FastAPI:
    app = FastAPI(
        title="Oreon Build Service",
        description="Production-grade Linux distro build system for oreon",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.exception_handler(OperationalError)
    async def _db_operational_error(request, exc: OperationalError):
        from fastapi.responses import JSONResponse

        if _too_many_connections(exc):
            logger.warning("database saturated path=%s", request.url.path)
            return JSONResponse(
                status_code=503,
                content={"detail": "database busy"},
                headers={"Retry-After": "5"},
            )
        raise exc

    @app.exception_handler(asyncpg.TooManyConnectionsError)
    async def _db_too_many_connections(request, exc: asyncpg.TooManyConnectionsError):
        from fastapi.responses import JSONResponse

        logger.warning("database saturated path=%s", request.url.path)
        return JSONResponse(
            status_code=503,
            content={"detail": "database busy"},
            headers={"Retry-After": "5"},
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(get_settings()),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _api_no_store(request, call_next):
        response = await call_next(request)
        path = request.url.path or ""
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response
    app.include_router(auth.router, prefix="/api")
    app.include_router(accounts.router, prefix="/api")
    app.include_router(releases.router, prefix="/api")
    app.include_router(packages.router, prefix="/api")
    app.include_router(builds.router, prefix="/api")
    app.include_router(workers.router, prefix="/api")
    app.include_router(mock_envs.router, prefix="/api")
    app.include_router(promotions.router, prefix="/api")
    app.include_router(side_tags.router, prefix="/api")
    app.include_router(iso_builds.router, prefix="/api")
    app.include_router(repos.router, prefix="/api")
    app.include_router(schedules.router, prefix="/api")
    app.include_router(logs.router, prefix="/api")
    app.include_router(audit.router, prefix="/api")
    app.include_router(search_router.router, prefix="/api")
    app.include_router(github_webhook.router, prefix="/api")
    app.include_router(worker_router, prefix="/api")
    web_dir = Path(__file__).resolve().parent.parent.parent.parent / "web"
    _no_cache_html = {"Cache-Control": "no-cache, must-revalidate"}

    def _html(path: Path) -> FileResponse:
        return FileResponse(path, headers=_no_cache_html)

    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(web_dir / "static")), name="static")
        @app.get("/")
        def _index():
            return _html(web_dir / "index.html")
        @app.get("/builds.html")
        def _builds():
            return _html(web_dir / "builds.html")
        @app.get("/build.html")
        def _build():
            return _html(web_dir / "build.html")
        @app.get("/packages.html")
        def _packages():
            return _html(web_dir / "packages.html")
        @app.get("/releases.html")
        def _releases():
            return _html(web_dir / "releases.html")
        @app.get("/workers.html")
        def _workers():
            return _html(web_dir / "workers.html")
        @app.get("/mock-environments.html")
        def _mock_envs():
            return _html(web_dir / "mock-environments.html")
        @app.get("/admin.html")
        def _admin():
            return _html(web_dir / "admin.html")
        @app.get("/repos.html")
        def _repos():
            return _html(web_dir / "repos.html")
        @app.get("/promotions.html")
        def _promotions():
            return _html(web_dir / "promotions.html")
        @app.get("/side-tags.html")
        def _side_tags():
            return _html(web_dir / "side-tags.html")
        @app.get("/side-tag.html")
        def _side_tag():
            return _html(web_dir / "side-tag.html")
        @app.get("/search.html")
        def _search():
            return _html(web_dir / "search.html")
        @app.get("/iso-builds.html")
        def _iso_builds():
            return _html(web_dir / "iso-builds.html")
        @app.get("/iso-build.html")
        def _iso_build():
            return _html(web_dir / "iso-build.html")
        @app.get("/security-finding.html")
        def _security_finding():
            return _html(web_dir / "security-finding.html")

        @app.get("/chains.html")
        def _chains_removed():
            return RedirectResponse(url="/side-tags.html", status_code=302)
        @app.get("/layers.html")
        def _layers_removed():
            return RedirectResponse(url="/side-tags.html", status_code=302)
    return app

app = create_app()

def run_server():
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "oreon_build.api.main:app",
        host="0.0.0.0",
        port=8000,
        log_level=settings.log_level.lower(),
        reload=False,
    )

if __name__ == "__main__":
    run_server()
