# Oreon Build Service
# Copyright (C) 2026 Oreon HQ
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolve a worker from X-Worker-Token without scanning the full workers table."""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oreon_build.config import get_settings
from oreon_build.core.security import verify_worker_token, worker_token_fingerprint
from oreon_build.models import Worker


async def resolve_worker_for_token(db: AsyncSession, token: str) -> Worker | None:
    """
    O(1) index lookup via token_fingerprint when set; legacy workers may scan rows with NULL
    fingerprint until a successful auth backfills the column.
    Bcrypt verification runs in a thread pool so worker traffic does not block the event loop.
    """
    if not token or not str(token).strip():
        return None
    secret = get_settings().secret_key
    fp = worker_token_fingerprint(token)

    r = await db.execute(select(Worker).where(Worker.token_fingerprint == fp))
    w = r.scalar_one_or_none()
    if w is not None:
        ok = await asyncio.to_thread(verify_worker_token, token, secret, w.token_hash)
        return w if ok else None

    r2 = await db.execute(select(Worker).where(Worker.token_fingerprint.is_(None)))
    for legacy in r2.scalars().all():
        ok = await asyncio.to_thread(verify_worker_token, token, secret, legacy.token_hash)
        if ok:
            legacy.token_fingerprint = fp
            await db.flush()
            return legacy
    return None
