from __future__ import annotations

import asyncio
import os
import time

import asyncpg


def _dsn() -> str:
    raw = (os.environ.get("DATABASE_URL") or "").strip()
    if not raw:
        raise RuntimeError("DATABASE_URL is not set")
    return raw.replace("+asyncpg", "")


async def main() -> int:
    dsn = _dsn()
    t0 = time.perf_counter()
    conn = await asyncpg.connect(dsn, timeout=8)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    try:
        db_name = await conn.fetchval("select current_database()")
        now = await conn.fetchval("select now()")

        maxc = int(await conn.fetchval("show max_connections"))
        current = int(
            await conn.fetchval(
                "select count(*)::int from pg_stat_activity where datname = current_database()"
            )
        )
        utilization = (current / maxc * 100.0) if maxc > 0 else 0.0

        super_reserved = int(await conn.fetchval("show superuser_reserved_connections"))

        blocked = int(
            await conn.fetchval(
                """
                select count(*)::int
                from pg_stat_activity
                where datname = current_database()
                  and wait_event_type = 'Lock'
                """
            )
        )

        groups = await conn.fetch(
            """
            select
              usename,
              coalesce(nullif(application_name, ''), '(none)') as app,
              coalesce(state, '(none)') as state,
              count(*)::int as n
            from pg_stat_activity
            where datname = current_database()
            group by usename, app, state
            order by n desc, usename, app, state
            limit 20
            """
        )

        print("db_health")
        print(f"  database={db_name}")
        print(f"  server_time={now}")
        print(f"  connect_latency_ms={latency_ms:.1f}")
        print(f"  max_connections={maxc}")
        print(f"  current_connections={current}")
        print(f"  utilization_pct={utilization:.1f}")
        print(f"  superuser_reserved_connections={super_reserved}")
        print(f"  blocked_lock_waiters={blocked}")
        print("  top_activity_groups")
        for r in groups:
            print(
                f"    user={r['usename']} app={r['app']} state={r['state']} n={r['n']}"
            )

        if utilization >= 90.0:
            print("status=CRITICAL")
        elif utilization >= 75.0:
            print("status=WARN")
        else:
            print("status=OK")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as e:
        print(f"db_health_error={e}")
        raise SystemExit(2)
