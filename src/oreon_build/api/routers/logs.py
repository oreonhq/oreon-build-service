# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from starlette.concurrency import iterate_in_threadpool

from oreon_build.db.session import async_session_maker
from oreon_build.models import BuildAttempt
from oreon_build.services.r2 import get_r2_client

router = APIRouter(prefix="/logs", tags=["logs"])

_TAIL_LINE_CAP = 5000
_TAIL_START_BYTES = 256 * 1024
_TAIL_MAX_BYTES = 8 * 1024 * 1024


def _read_log_tail(key: str, lines: int) -> list[str] | None:
    r2 = get_r2_client()
    size = r2.object_content_length(key)
    if size is None:
        return None
    if size == 0:
        return []
    window = min(size, _TAIL_START_BYTES)
    while True:
        start = size - window
        data = r2.get_object_range(key, start, size - 1)
        if data is None:
            return None
        text = data.decode("utf-8", errors="replace")
        if start > 0:
            text = text.split("\n", 1)[-1]
        parts = text.splitlines()
        if len(parts) >= lines or start == 0 or window >= size:
            return parts[-lines:]
        window = min(size, window * 4, _TAIL_MAX_BYTES)
        if window >= _TAIL_MAX_BYTES and start > 0:
            return parts[-lines:]


def _iter_log_chunks(key: str):
    r2 = get_r2_client()
    stream = r2.get_object_stream(key)
    if stream is None:
        return None

    def chunks():
        try:
            for chunk in stream.iter_chunks(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()

    return chunks()


@router.get("/attempts/{attempt_id}")
async def get_build_log(attempt_id: int):
    async with async_session_maker() as db:
        result = await db.execute(select(BuildAttempt.log_r2_key).where(BuildAttempt.id == attempt_id))
        log_r2_key = result.scalar_one_or_none()
        if log_r2_key is None:
            exists = await db.execute(select(BuildAttempt.id).where(BuildAttempt.id == attempt_id))
            if exists.scalar_one_or_none() is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build attempt not found")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Log not yet available")

    iterator = await asyncio.to_thread(_iter_log_chunks, log_r2_key)
    if iterator is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Log file not found in storage")
    return StreamingResponse(
        iterate_in_threadpool(iterator),
        media_type="text/plain",
        headers={"Content-Disposition": "inline; filename=build.log"},
    )


@router.get("/attempts/{attempt_id}/tail")
async def get_build_log_tail(attempt_id: int, lines: int = 100):
    if lines < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="lines must be at least 1")
    if lines > _TAIL_LINE_CAP:
        lines = _TAIL_LINE_CAP
    async with async_session_maker() as db:
        result = await db.execute(select(BuildAttempt.log_r2_key).where(BuildAttempt.id == attempt_id))
        log_r2_key = result.scalar_one_or_none()
        if log_r2_key is None:
            exists = await db.execute(select(BuildAttempt.id).where(BuildAttempt.id == attempt_id))
            if exists.scalar_one_or_none() is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build attempt not found")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Log not yet available")

    tail_lines = await asyncio.to_thread(_read_log_tail, log_r2_key, lines)
    if tail_lines is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Log file not found in storage")
    return JSONResponse(
        content={"lines": tail_lines},
        headers={"Cache-Control": "no-store"},
    )
