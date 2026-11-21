# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from starlette.concurrency import iterate_in_threadpool

from oreon_build.db.session import async_session_maker
from oreon_build.models import BuildAttempt, BuildJob, Release
from oreon_build.services.build_logs import safe_log_name, sort_log_names
from oreon_build.services.r2 import get_r2_client, log_file_r2_key, log_files_prefix

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


async def _attempt_releasename(attempt_id: int) -> str:
    async with async_session_maker() as db:
        row = await db.execute(
            select(Release.releasename)
            .join(BuildJob, BuildJob.release_id == Release.id)
            .join(BuildAttempt, BuildAttempt.build_job_id == BuildJob.id)
            .where(BuildAttempt.id == attempt_id)
        )
        name = row.scalar_one_or_none()
    if name is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build attempt not found")
    return name


@router.get("/attempts/{attempt_id}/files")
async def list_attempt_logs(attempt_id: int):
    releasename = await _attempt_releasename(attempt_id)
    prefix = log_files_prefix(releasename, attempt_id)

    def _list() -> list[dict]:
        objs = get_r2_client().list_objects(prefix)
        files = []
        for obj in objs:
            key = str(obj.get("key") or "")
            if not key.startswith(prefix):
                continue
            rel = key[len(prefix):]
            safe = safe_log_name(rel)
            if safe is None:
                continue
            files.append({"name": safe, "size": int(obj.get("size") or 0)})
        order = {name: i for i, name in enumerate(sort_log_names([f["name"] for f in files]))}
        files.sort(key=lambda item: order.get(item["name"], 0))
        return files

    files = await asyncio.to_thread(_list)
    return JSONResponse(content={"files": files}, headers={"Cache-Control": "no-store"})


@router.get("/attempts/{attempt_id}/files/{name:path}")
async def get_attempt_log_file(attempt_id: int, name: str):
    safe = safe_log_name(name)
    if safe is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Log not found")
    releasename = await _attempt_releasename(attempt_id)
    key = log_file_r2_key(releasename, attempt_id, safe)
    iterator = await asyncio.to_thread(_iter_log_chunks, key)
    if iterator is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Log file not found in storage")
    base = safe.rsplit("/", 1)[-1]
    return StreamingResponse(
        iterate_in_threadpool(iterator),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="{base}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
