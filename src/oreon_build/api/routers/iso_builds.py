from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from oreon_build.api.deps import CurrentUser, DbSession
from oreon_build.config import get_settings
from oreon_build.models import BuildStatus, IsoBuild, MockEnvironment
from oreon_build.services.iso_builds import list_kickstart_tree, mock_config_for_arch, validate_iso_request
from oreon_build.services.r2 import get_r2_client

router = APIRouter(prefix="/iso-builds", tags=["iso-builds"])


class IsoBuildCreate(BaseModel):
    mock_environment_id: int = Field(..., ge=1)
    release_folder: str
    arches: list[str]
    kickstarts: dict[str, str]
    iso_name: str
    project: str = "Oreon"
    volid: str
    releasever: str
    macboot: bool = False


def _row(job: IsoBuild) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "architecture": job.architecture,
        "mock_environment_id": job.mock_environment_id,
        "mock_config": job.mock_config,
        "release_folder": job.release_folder,
        "kickstart": job.kickstart,
        "iso_name": job.iso_name,
        "project": job.project,
        "volid": job.volid,
        "releasever": job.releasever,
        "macboot": job.macboot,
        "sha256": job.sha256,
        "size_bytes": job.size_bytes,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "completed_at": job.completed_at,
        "error_message": job.error_message,
    }


@router.get("/kickstarts")
async def kickstarts(folder: str | None = None):
    try:
        return list_kickstart_tree(folder)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Kickstart repo lookup failed: {exc}") from exc


@router.get("")
async def list_iso_builds(db: DbSession, limit: int = 50, offset: int = 0):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    total = int((await db.scalar(select(func.count(IsoBuild.id)))) or 0)
    rows = list(
        (
            await db.execute(select(IsoBuild).order_by(IsoBuild.id.desc()).offset(offset).limit(limit))
        ).scalars().all()
    )
    return {"items": [_row(j) for j in rows], "total": total}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_iso_builds(data: IsoBuildCreate, db: DbSession, user: CurrentUser):
    try:
        specs = validate_iso_request(
            release_folder=data.release_folder,
            arches=data.arches,
            kickstarts=data.kickstarts,
            iso_name=data.iso_name,
            project=data.project,
            volid=data.volid,
            releasever=data.releasever,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    mock = await db.get(MockEnvironment, data.mock_environment_id)
    if mock is None or not mock.is_available:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mock environment not found")
    created = []
    for spec in specs:
        job = IsoBuild(
            status=BuildStatus.PENDING.value,
            architecture=spec["architecture"],
            mock_environment_id=mock.id,
            mock_config=mock_config_for_arch(mock.config_name, spec["architecture"]),
            release_folder=spec["release_folder"],
            kickstart=spec["kickstart"],
            iso_name=spec["iso_name"],
            project=spec["project"],
            volid=spec["volid"],
            releasever=spec["releasever"],
            macboot=bool(data.macboot),
            triggered_by_id=user.id,
        )
        db.add(job)
        created.append(job)
    await db.flush()
    return {"items": [_row(j) for j in created]}


@router.get("/{iso_id}")
async def get_iso_build(iso_id: int, db: DbSession):
    job = await db.get(IsoBuild, iso_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ISO build not found")
    return _row(job)


@router.get("/{iso_id}/log")
async def iso_log(iso_id: int, db: DbSession):
    job = await db.get(IsoBuild, iso_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ISO build not found")
    return {"log": job.log_text or "", "status": job.status}


@router.get("/{iso_id}/download")
async def iso_download(iso_id: int, db: DbSession):
    job = await db.get(IsoBuild, iso_id)
    if job is None or job.status != BuildStatus.SUCCESS.value or not job.r2_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ISO is not ready")
    url = get_r2_client().generate_presigned_url_for_bucket(
        get_settings().mirrorlist_r2_bucket,
        job.r2_key,
        method="get_object",
        expires_in=3600,
    )
    return RedirectResponse(url, status_code=302)


@router.post("/{iso_id}/cancel")
async def cancel_iso(iso_id: int, db: DbSession, user: CurrentUser):
    job = await db.get(IsoBuild, iso_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ISO build not found")
    if job.status in (BuildStatus.SUCCESS.value, BuildStatus.FAILED.value, BuildStatus.CANCELLED.value):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ISO build already finished")
    job.status = BuildStatus.CANCELLED.value
    job.completed_at = datetime.now(timezone.utc)
    job.error_message = "Cancelled by user"
    await db.flush()
    return _row(job)
