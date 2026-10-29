# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""SQLite-backed BR/provides cache keyed by (package, content_key).

content_key stands in for a per-file git commit sha. Specs are fetched from
distgit by branch name (raw.githubusercontent.com), not by blob sha, so
getting a real per-package commit sha would mean an extra GitHub API call
per package just to populate a cache key - that network round trip would
cost more than the rpmbuild -br run we're trying to avoid. Hashing the
spec text plus build defines plus local source file stats gives the same
invalidation guarantee (any real change busts the cache) with zero extra
network cost.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_VERSION = 1
_LOCK = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db_path() -> Path:
    raw = (os.getenv("OREON_BR_SQLITE_PATH") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".cache" / "oreon-build" / f"br_cache_v{_DB_VERSION}.sqlite3"


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS br_cache (
            pkg TEXT NOT NULL,
            content_key TEXT NOT NULL,
            buildrequires TEXT NOT NULL,
            provides TEXT NOT NULL,
            complete INTEGER NOT NULL,
            error TEXT NOT NULL,
            pass_kind TEXT NOT NULL,
            ts REAL NOT NULL,
            PRIMARY KEY (pkg, content_key)
        )
        """
    )
    conn.commit()
    _conn = conn
    return conn


def get(pkg: str, content_key: str) -> dict | None:
    try:
        with _LOCK:
            conn = _get_conn()
            row = conn.execute(
                "SELECT buildrequires, provides, complete, error, pass_kind "
                "FROM br_cache WHERE pkg=? AND content_key=?",
                (pkg, content_key),
            ).fetchone()
    except Exception:
        logger.debug("br sqlite cache read failed", exc_info=True)
        return None
    if not row:
        return None
    brs_json, provides_json, complete, error, pass_kind = row
    try:
        brs = json.loads(brs_json)
        provides = json.loads(provides_json)
    except Exception:
        return None
    if not isinstance(brs, list):
        return None
    return {
        "buildrequires": [str(x) for x in brs],
        "provides": [str(x) for x in provides] if isinstance(provides, list) else [],
        "complete": bool(complete),
        "error": str(error or ""),
        "pass_kind": str(pass_kind or "cached"),
    }


def put(
    pkg: str,
    content_key: str,
    *,
    buildrequires: list[str],
    provides: list[str] | None = None,
    complete: bool,
    error: str = "",
    pass_kind: str = "final",
) -> None:
    try:
        with _LOCK:
            conn = _get_conn()
            conn.execute(
                """
                INSERT INTO br_cache
                    (pkg, content_key, buildrequires, provides, complete, error, pass_kind, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pkg, content_key) DO UPDATE SET
                    buildrequires=excluded.buildrequires,
                    provides=excluded.provides,
                    complete=excluded.complete,
                    error=excluded.error,
                    pass_kind=excluded.pass_kind,
                    ts=excluded.ts
                """,
                (
                    pkg,
                    content_key,
                    json.dumps(list(buildrequires)),
                    json.dumps(list(provides or [])),
                    1 if complete else 0,
                    error or "",
                    pass_kind or "final",
                    time.monotonic(),
                ),
            )
            conn.commit()
    except Exception:
        logger.debug("br sqlite cache write failed", exc_info=True)


def clear() -> None:
    with _LOCK:
        conn = _get_conn()
        conn.execute("DELETE FROM br_cache")
        conn.commit()
