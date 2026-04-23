"""Dependencies graph and missing dependency diagnostics."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from oreon_build.api.deps import DbSession
from oreon_build.services.dependencies_graph import build_dependencies_graph

router = APIRouter(prefix="/dependencies", tags=["dependencies"])


@router.get("/graph", response_model=dict)
async def dependencies_graph(db: DbSession) -> dict:
    try:
        return await build_dependencies_graph(db)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
