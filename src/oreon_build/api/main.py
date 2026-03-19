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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from oreon_build.config import get_settings
from oreon_build.db import get_db, init_db
from oreon_build.models import Account, Role, RoleName
from oreon_build.core.security import hash_password

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
    gitlab_webhook,
)
from .worker_api import router as worker_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    settings = get_settings()
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
                if not result.scalar_one_or_none():
                    admin = Account(
                        username=settings.admin_username,
                        password_hash=hash_password(settings.admin_password),
                        role_id=admin_role.id,
                        is_active=True,
                    )
                    db.add(admin)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    yield
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
    app.include_router(gitlab_webhook.router, prefix="/api")
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
