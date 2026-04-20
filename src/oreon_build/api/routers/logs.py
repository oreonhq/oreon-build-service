"""Build logs - stream or fetch from R2 (read-only)."""
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from oreon_build.db.session import async_session_maker
from oreon_build.models import BuildAttempt
from oreon_build.services.r2 import get_r2_client

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("/attempts/{attempt_id}")
async def get_build_log(attempt_id: int):
    # Use explicit session management to release DB connection before streaming
    async with async_session_maker() as db:
        result = await db.execute(select(BuildAttempt).where(BuildAttempt.id == attempt_id))
        attempt = result.scalar_one_or_none()
        if not attempt:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build attempt not found")
        if not attempt.log_r2_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Log not yet available")
        log_r2_key = attempt.log_r2_key

    r2 = get_r2_client()
    stream = r2.get_object_stream(log_r2_key)
    if stream is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Log file not found in storage")
    return StreamingResponse(
        stream.iter_chunks(chunk_size=65536),
        media_type="text/plain",
        headers={"Content-Disposition": "inline; filename=build.log"},
    )


@router.get("/attempts/{attempt_id}/tail")
async def get_build_log_tail(attempt_id: int, lines: int = 100):
    async with async_session_maker() as db:
        result = await db.execute(select(BuildAttempt).where(BuildAttempt.id == attempt_id))
        attempt = result.scalar_one_or_none()
        if not attempt:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build attempt not found")
        if not attempt.log_r2_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Log not yet available")
        log_r2_key = attempt.log_r2_key
        if lines < 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="lines must be at least 1")

    r2 = get_r2_client()
    data = r2.get_object(log_r2_key)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Log file not found in storage")
    text = data.decode("utf-8", errors="replace")
    tail_lines = text.splitlines()[-lines:]
    return {"lines": tail_lines}
