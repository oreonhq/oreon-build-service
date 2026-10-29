# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg


def _dsn() -> str:
    dsn = (
        os.environ.get("DATABASE_ADMIN_URL")
        or os.environ.get("DATABASE_RECLAIM_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    if not dsn:
        raise RuntimeError("Set DATABASE_URL.")
    return dsn.replace("+asyncpg", "")


async def main() -> int:
    dsn = _dsn()
    idle_age = int((os.environ.get("OREON_DB_UNCLOG_IDLE_SECONDS") or "180").strip() or "180")
    idle_age = max(30, min(idle_age, 3600))
    max_kill = int((os.environ.get("OREON_DB_UNCLOG_MAX_KILL") or "40").strip() or "40")
    max_kill = max(1, min(max_kill, 500))
    connect_retries = int((os.environ.get("OREON_DB_UNCLOG_CONNECT_RETRIES") or "12").strip() or "12")
    connect_retries = max(1, min(connect_retries, 120))
    connect_sleep = float((os.environ.get("OREON_DB_UNCLOG_CONNECT_SLEEP_SEC") or "2").strip() or "2")
    connect_sleep = max(0.2, min(connect_sleep, 30.0))

    conn = None
    last_err: BaseException | None = None
    for i in range(1, connect_retries + 1):
        try:
            conn = await asyncpg.connect(dsn, timeout=8)
            break
        except Exception as e:
            last_err = e
            if isinstance(e, asyncpg.TooManyConnectionsError) and i < connect_retries:
                await asyncio.sleep(connect_sleep)
                continue
            raise
    if conn is None:
        if last_err is not None:
            raise last_err
        raise RuntimeError("could not connect")
    try:
        dbname = await conn.fetchval("select current_database()")
        print(
            f"db_unclog database={dbname} idle_threshold_s={idle_age} "
            f"max_kill={max_kill} connect_retries={connect_retries}"
        )

        rows = await conn.fetch(
            """
            select
              usename,
              coalesce(nullif(application_name, ''), '(none)') as app,
              state,
              count(*)::int as n
            from pg_stat_activity
            where datname = current_database()
            group by usename, app, state
            order by n desc, usename, app, state
            """
        )
        print("connections_by_user_app_state")
        for r in rows:
            print(f"  user={r['usename']} app={r['app']} state={r['state']} n={r['n']}")

        me_user = await conn.fetchval("select current_user")
        print(f"connected_as={me_user}")

        killed = await conn.fetch(
            """
            with victims as (
              select pid
              from pg_stat_activity
              where datname = current_database()
                and pid <> pg_backend_pid()
                and state in ('idle', 'idle in transaction')
                and now() - coalesce(state_change, backend_start) > ($1::text || ' seconds')::interval
              order by state_change asc nulls first
              limit $2
            )
            select pid, pg_terminate_backend(pid) as terminated
            from victims
            """
            ,
            str(idle_age),
            max_kill,
        )
        n = sum(1 for r in killed if r["terminated"])
        print(f"terminated_idle_backends={n}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as e:
        print(f"db_unclog failed: {e}", file=sys.stderr)
        if isinstance(e, asyncpg.TooManyConnectionsError):
            raise SystemExit(3)
        raise SystemExit(1)
