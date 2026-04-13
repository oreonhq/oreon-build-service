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

"""Oreon Build Service API - main application."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from oreon_build.config import Settings, get_settings
from oreon_build.db import get_db, init_db
from oreon_build.models import Account, Role, RoleName
from oreon_build.core.security import hash_password, verify_password

logger = logging.getLogger(__name__)

_github_catchup_misconfig_logged = False


def _configure_oreon_logging(settings: Settings) -> None:
    """Ensure oreon_build log lines reach stderr (uvicorn often leaves app loggers quiet)."""
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
)
from .worker_api import router as worker_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    settings = get_settings()
    _configure_oreon_logging(settings)
    logger.info(
        "API startup log_level=%s github_catchup_s=%s github_token=%s",
        settings.log_level,
        settings.github_catchup_interval_seconds,
        bool(settings.github_token),
    )
    from oreon_build.db.session import async_session_maker
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
                    # First boot: create the default admin from env.
                    admin = Account(
                        username=settings.admin_username,
                        password_hash=hash_password(settings.admin_password),
                        role_id=admin_role.id,
                        is_active=True,
                    )
                    db.add(admin)
                else:
                    # Subsequent boots: if the env password changed, update the stored
                    # password hash (the "invalid password" symptom many users hit).
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

    sweep_task: asyncio.Task[None] | None = None
    publish_task: asyncio.Task[None] | None = None

    async def _stale_build_sweep_loop() -> None:
        from oreon_build.db.session import async_session_maker
        from oreon_build.services.stale_builds import sweep_stale_running_builds

        while True:
            try:
                s = get_settings()
                interval = max(60, s.stale_build_sweep_interval_seconds)
                if s.build_stale_running_minutes > 0 or s.build_stale_worker_heartbeat_minutes > 0:
                    async with async_session_maker() as db:
                        try:
                            await sweep_stale_running_builds(db, s)
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
                if s.github_catchup_interval_seconds <= 0 or not s.github_token:
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
        from datetime import datetime, timezone

        from oreon_build.core.repo_branch import r2_publish_branch
        from oreon_build.db.session import async_session_maker
        from oreon_build.models import Artifact, BuildAttempt, BuildJob, BuildTarget, PublishTask, Release
        from oreon_build.services.publisher import update_repodata_at_prefix
        from oreon_build.services.r2 import get_r2_client, repo_r2_prefix, repo_rpms_key, src_r2_key
        from oreon_build.api.worker_api import ArtifactPayload, _prune_superseded_repo_objects

        def _process_repo_publish_sync(
            *,
            releasename: str,
            branch: str,
            arch: str,
            attempt_id: int,
            payload_artifacts: list[ArtifactPayload],
            build_attempt_r2_keys: list[str],
        ) -> None:
            r2 = get_r2_client()

            # If worker uploaded into builds/ keys, copy into repo layout first.
            for a in payload_artifacts:
                if a.r2_key and "/builds/" in a.r2_key and a.filename:
                    data = r2.get_object(a.r2_key)
                    if not data:
                        continue
                    if a.filename.endswith(".src.rpm"):
                        dest = src_r2_key(releasename, branch, a.filename)
                    else:
                        dest = repo_rpms_key(releasename, branch, arch, a.filename)
                    r2.put_object(dest, data)

            keep = {a.filename for a in payload_artifacts if a.filename}
            _prune_superseded_repo_objects(
                releasename=releasename,
                branch=branch,
                arch=arch,
                keep_artifact_filenames=keep,
                payload_artifacts=payload_artifacts,
            )

            prefix = repo_r2_prefix(releasename, branch, arch)
            update_repodata_at_prefix(prefix)

            exact_builds_prefix = f"{releasename}/builds/{attempt_id}/"
            for key in build_attempt_r2_keys:
                if key and key.startswith(exact_builds_prefix):
                    try:
                        r2.delete_object(key)
                    except Exception:
                        pass

        while True:
            try:
                raw_idle = (os.environ.get("OREON_PUBLISH_TASK_IDLE_POLL_SEC") or "2").strip()
                try:
                    idle_interval_s = float(raw_idle)
                except ValueError:
                    idle_interval_s = 2.0
                idle_interval_s = max(0.5, min(idle_interval_s, 30.0))
                batch = 5
                async with async_session_maker() as db:
                    now = datetime.now(timezone.utc)
                    q = (
                        select(PublishTask)
                        .where(PublishTask.status == "pending")
                        .order_by(PublishTask.created_at.asc())
                        .limit(batch)
                        .with_for_update(skip_locked=True)
                    )
                    result = await db.execute(q)
                    tasks = list(result.scalars().all())
                    if not tasks:
                        await asyncio.sleep(idle_interval_s)
                        continue
                    for t in tasks:
                        t.status = "running"
                        t.started_at = now
                    await db.commit()

                for t in tasks:
                    attempt_id = t.attempt_id
                    try:
                        async with async_session_maker() as db2:
                            job_id = t.build_job_id

                            job_r = await db2.execute(select(BuildJob).where(BuildJob.id == job_id))
                            job = job_r.scalar_one_or_none()
                            if not job:
                                raise RuntimeError(f"build job not found: {job_id}")

                            rel_r = await db2.execute(select(Release).where(Release.id == job.release_id))
                            release = rel_r.scalar_one_or_none()
                            if not release:
                                raise RuntimeError(f"release not found: {job.release_id}")

                            if not job.target_id:
                                raise RuntimeError(f"build target missing for job: {job_id}")
                            target_r = await db2.execute(
                                select(BuildTarget).where(BuildTarget.id == job.target_id)
                            )
                            target = target_r.scalar_one_or_none()
                            if not target or not target.architecture:
                                raise RuntimeError(f"build target arch missing for job: {job_id}")

                            branch = r2_publish_branch(release)
                            arch = target.architecture

                            art_r = await db2.execute(
                                select(Artifact).where(Artifact.build_attempt_id == attempt_id)
                            )
                            artifacts = list(art_r.scalars().all())
                            payload_artifacts = [
                                ArtifactPayload(
                                    kind=a.kind,
                                    filename=a.filename,
                                    r2_key=a.r2_key,
                                    size_bytes=a.size_bytes,
                                    checksum_sha256=a.checksum_sha256,
                                    signed=bool(a.is_signed),
                                )
                                for a in artifacts
                            ]
                            r2_keys = [a.r2_key for a in artifacts if a.r2_key]

                        await asyncio.to_thread(
                            _process_repo_publish_sync,
                            releasename=release.releasename,
                            branch=branch,
                            arch=arch,
                            attempt_id=attempt_id,
                            payload_artifacts=payload_artifacts,
                            build_attempt_r2_keys=r2_keys,
                        )

                        async with async_session_maker() as db3:
                            task_row = await db3.get(PublishTask, t.id)
                            if task_row:
                                task_row.status = "success"
                                task_row.finished_at = datetime.now(timezone.utc)
                                task_row.error_message = None
                                await db3.flush()
                                await db3.commit()
                    except Exception as e:
                        async with async_session_maker() as db3:
                            task_row = await db3.get(PublishTask, t.id)
                            if task_row:
                                task_row.status = "failed"
                                task_row.finished_at = datetime.now(timezone.utc)
                                task_row.error_message = str(e)
                                await db3.flush()
                                await db3.commit()
                        logger.exception(
                            "Publish task failed id=%s attempt_id=%s job_id=%s",
                            t.id,
                            t.attempt_id,
                            t.build_job_id,
                        )

                # Don't sleep after successful processing: immediately check for more pending rows
                # so repodata refresh starts as soon as artifacts are queued.
                if len(tasks) < batch:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Publish tasks loop error")
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    break

    publish_task = asyncio.create_task(_publish_tasks_loop(), name="publish-tasks")

    yield

    for t, label in [(sweep_task, "stale-build-sweep"), (catchup_task, "github-push-catchup"), (publish_task, "publish-tasks")]:
        if t is not None:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    # shutdown


def create_app() -> FastAPI:
    app = FastAPI(
        title="Oreon Build Service",
        description="Production-grade Linux distro build system for oreon",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth.router, prefix="/api")
    app.include_router(accounts.router, prefix="/api")
    app.include_router(releases.router, prefix="/api")
    app.include_router(packages.router, prefix="/api")
    app.include_router(builds.router, prefix="/api")
    app.include_router(workers.router, prefix="/api")
    app.include_router(mock_envs.router, prefix="/api")
    app.include_router(promotions.router, prefix="/api")
    app.include_router(repos.router, prefix="/api")
    app.include_router(schedules.router, prefix="/api")
    app.include_router(logs.router, prefix="/api")
    app.include_router(audit.router, prefix="/api")
    app.include_router(search_router.router, prefix="/api")
    app.include_router(github_webhook.router, prefix="/api")
    app.include_router(worker_router, prefix="/api")
    # api/main.py -> oreon_build -> src -> repo root
    web_dir = Path(__file__).resolve().parent.parent.parent.parent / "web"
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(web_dir / "static")), name="static")
        @app.get("/")
        def _index():
            return FileResponse(web_dir / "index.html")
        @app.get("/builds.html")
        def _builds():
            return FileResponse(web_dir / "builds.html")
        @app.get("/failures.html")
        def _failures():
            return FileResponse(web_dir / "failures.html")
        @app.get("/build.html")
        def _build():
            return FileResponse(web_dir / "build.html")
        @app.get("/packages.html")
        def _packages():
            return FileResponse(web_dir / "packages.html")
        @app.get("/releases.html")
        def _releases():
            return FileResponse(web_dir / "releases.html")
        @app.get("/workers.html")
        def _workers():
            return FileResponse(web_dir / "workers.html")
        @app.get("/mock-environments.html")
        def _mock_envs():
            return FileResponse(web_dir / "mock-environments.html")
        @app.get("/admin.html")
        def _admin():
            return FileResponse(web_dir / "admin.html")
        @app.get("/repos.html")
        def _repos():
            return FileResponse(web_dir / "repos.html")
        @app.get("/promotions.html")
        def _promotions():
            return FileResponse(web_dir / "promotions.html")
        @app.get("/search.html")
        def _search():
            return FileResponse(web_dir / "search.html")
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
