#!/usr/bin/env python3
"""One-shot: reset publish_tasks stuck in ``running`` (same logic as the API stale sweep).

Run from repo root with the project venv active (``pip install -e .`` or ``PYTHONPATH=src``).

If Postgres returns "remaining connection slots are reserved for roles with the SUPERUSER attribute",
the app role cannot open a new session. Set DATABASE_RECLAIM_URL to an async superuser URL for the
same database so this script can still connect, reclaim stuck rows, and free logical queue blockage.
"""
from __future__ import annotations

import asyncio
import sys

import asyncpg
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from oreon_build.config import Settings, get_settings
from oreon_build.services.stale_publish_tasks import reclaim_publish_tasks_stuck_running


def _too_many_connections(exc: BaseException) -> bool:
    if isinstance(exc, asyncpg.TooManyConnectionsError):
        return True
    if isinstance(exc, OperationalError):
        c = exc.__cause__
        return isinstance(c, asyncpg.TooManyConnectionsError)
    return False


def _exception_chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in out:
        out.append(cur)
        cur = cur.__cause__
    return out


def _invalid_password(exc: BaseException) -> bool:
    return any(isinstance(x, asyncpg.InvalidPasswordError) for x in _exception_chain(exc))


def _url_role_key(database_url: str) -> tuple[str, str, int, str | None]:
    """host, user, port, database for comparing whether two URLs target the same DB role."""
    u = make_url(database_url)
    host = (u.host or "").lower()
    user = (u.username or "").lower()
    port = int(u.port or 5432)
    return (host, user, port, u.database)


def _reclaim_url_same_role_as_primary(primary_url: str, reclaim_url: str) -> bool:
    return _url_role_key(primary_url) == _url_role_key(reclaim_url)


def _server_settings(settings: Settings, application_name: str) -> dict[str, str]:
    name = application_name[:128]
    d: dict[str, str] = {"application_name": name}
    if settings.database_idle_in_transaction_timeout_seconds > 0:
        d["idle_in_transaction_session_timeout"] = str(
            settings.database_idle_in_transaction_timeout_seconds * 1000
        )
    return d


# Do not hang start.sh on wedged Postgres or long retry loops.
_RECLAIM_CONNECT_TIMEOUT_S = 8.0


async def _reclaim_with_url(database_url: str, settings: Settings, application_name: str) -> int:
    engine = create_async_engine(
        database_url,
        echo=False,
        poolclass=NullPool,
        connect_args={
            "timeout": _RECLAIM_CONNECT_TIMEOUT_S,
            "server_settings": _server_settings(settings, application_name),
        },
    )
    try:
        sm = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
        async with sm() as db:
            n = await reclaim_publish_tasks_stuck_running(db, settings)
            await db.commit()
        return n
    finally:
        await engine.dispose()


async def _try_url(
    database_url: str,
    settings: Settings,
    application_name: str,
    *,
    attempts: int,
    initial_sleep_s: float,
    label_for_stdout: str,
) -> bool:
    sleep_s = initial_sleep_s
    for attempt in range(1, attempts + 1):
        try:
            n = await _reclaim_with_url(database_url, settings, application_name)
            suffix = f" ({label_for_stdout})" if label_for_stdout else ""
            print(f"reclaimed_publish_tasks={n}{suffix}")
            return True
        except Exception as e:
            if _invalid_password(e):
                raise
            if not _too_many_connections(e):
                raise
            if attempt == attempts:
                return False
            await asyncio.sleep(min(sleep_s, 2.0))
            sleep_s = min(sleep_s * 1.5, 2.0)
    return False


async def main() -> int:
    s = get_settings()
    base = (s.database_application_name or "oreon-build-service")[:100]

    # Few short waits (post-stop drain), then fall through to DATABASE_RECLAIM_URL or fail fast.
    if await _try_url(
        s.database_url,
        s,
        f"{base}-reclaim",
        attempts=5,
        initial_sleep_s=0.2,
        label_for_stdout="",
    ):
        return 0

    alt = (s.database_reclaim_url or "").strip()
    if not alt:
        print(
            "reclaim: could not connect (too many connections for DATABASE_URL). "
            "Set DATABASE_RECLAIM_URL to postgresql+asyncpg://postgres:PASSWORD@HOST:PORT/DBNAME "
            "for the same database as DATABASE_URL, or free Postgres connections (pg_stat_activity), "
            "then run again.",
            file=sys.stderr,
        )
        return 1

    if _reclaim_url_same_role_as_primary(s.database_url, alt):
        print(
            "reclaim: DATABASE_RECLAIM_URL uses the same PostgreSQL user and database as DATABASE_URL.",
            file=sys.stderr,
        )
        print(
            "reclaim: A second URL for the same role cannot bypass that role's connection limit. "
            "Point DATABASE_RECLAIM_URL at a superuser (often user postgres) on the same database, "
            "or terminate other sessions and retry.",
            file=sys.stderr,
        )
        return 1

    if await _try_url(
        alt,
        s,
        f"{base}-reclaim-admin",
        attempts=5,
        initial_sleep_s=0.25,
        label_for_stdout="DATABASE_RECLAIM_URL",
    ):
        return 0

    print(
        "reclaim: DATABASE_RECLAIM_URL also could not connect after retries. "
        "Check max_connections, terminate idle sessions, or raise Postgres limits.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as e:
        if _invalid_password(e):
            print(
                "reclaim: PostgreSQL rejected the password. "
                "Fix DATABASE_URL, or fix DATABASE_RECLAIM_URL, or remove DATABASE_RECLAIM_URL if you do not use the superuser fallback.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        raise
