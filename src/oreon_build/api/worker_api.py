"""Worker-facing API: poll job, heartbeat, upload RPMs for signing, report result."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select, func

from oreon_build.api.deps import DbSession
from oreon_build.config import get_settings
from oreon_build.core.security import verify_worker_token
from oreon_build.core.audit import log_audit
from sqlalchemy.orm import selectinload

from oreon_build.models import (
    Artifact,
    BuildAttempt,
    BuildJob,
    BuildStatus,
    BuildTarget,
    MockEnvironment,
    Package,
    Promotion,
    Release,
    Source,
    Worker,
    WorkerState,
)
from oreon_build.services.r2 import get_r2_client, repo_r2_prefix, repo_rpms_key, src_r2_key
from oreon_build.services.publisher import update_repodata_at_prefix
from oreon_build.services.signing import sign_rpm

router = APIRouter(prefix="/worker", tags=["worker"])
logger = logging.getLogger(__name__)


async def get_worker(
    db: DbSession,
    x_worker_token: Annotated[str | None, Header(alias="X-Worker-Token")] = None,
) -> Worker:
    if not x_worker_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-Worker-Token required")
    settings = get_settings()
    result = await db.execute(select(Worker))
    for w in result.scalars().all():
        if verify_worker_token(x_worker_token, settings.secret_key, w.token_hash):
            w.last_seen_at = datetime.now(timezone.utc)
            w.last_heartbeat = datetime.now(timezone.utc)
            await db.flush()
            return w
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token")


async def _require_worker(
    db: DbSession,
    x_worker_token: Annotated[str | None, Header(alias="X-Worker-Token")] = None,
) -> Worker:
    if not x_worker_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-Worker-Token required")
    settings = get_settings()
    result = await db.execute(select(Worker))
    for w in result.scalars().all():
        if verify_worker_token(x_worker_token, settings.secret_key, w.token_hash):
            w.last_seen_at = datetime.now(timezone.utc)
            w.last_heartbeat = datetime.now(timezone.utc)
            await db.flush()
            return w
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token")


class ResultPayload(BaseModel):
    status: str
    error_message: str | None = None
    log_r2_key: str | None = None
    artifacts: list[ArtifactPayload] = []


class ArtifactPayload(BaseModel):
    kind: str
    filename: str
    r2_key: str
    size_bytes: int | None = None
    checksum_sha256: str | None = None
    signed: bool | None = None


@router.post("/heartbeat")
async def heartbeat(db: DbSession, worker: Annotated[Worker, Depends(_require_worker)]):
    worker.last_heartbeat = datetime.now(timezone.utc)
    worker.last_seen_at = datetime.now(timezone.utc)
    if worker.state == WorkerState.OFFLINE:
        worker.state = WorkerState.IDLE
    await db.flush()
    return {"status": "ok", "state": worker.state.value}


@router.get("/poll")
async def poll_job(db: DbSession, worker: Annotated[Worker, Depends(_require_worker)]):
    if worker.state == WorkerState.OFFLINE:
        worker.state = WorkerState.IDLE
        await db.flush()
    if worker.state not in (WorkerState.IDLE, WorkerState.BUSY):
        return {"job": None}
    # Only return jobs whose target architecture matches this worker (or job has no target for backward compat)
    worker_arch = (worker.architecture or "").strip() or None
    q = (
        select(BuildJob)
        .where(BuildJob.status.in_([BuildStatus.PENDING, BuildStatus.QUEUED]))
        .order_by(BuildJob.priority.desc(), BuildJob.created_at.asc())
    )
    if worker_arch:
        q = q.outerjoin(BuildTarget, BuildJob.target_id == BuildTarget.id).where(
            (BuildJob.target_id.is_(None)) | (BuildTarget.architecture == worker_arch)
        )
    result = await db.execute(q.limit(1))
    job = result.scalar_one_or_none()
    if not job:
        return {"job": None}
    job.status = BuildStatus.RUNNING
    count_result = await db.execute(
        select(func.count(BuildAttempt.id)).where(BuildAttempt.build_job_id == job.id)
    )
    attempt_number = (count_result.scalar() or 0) + 1
    attempt = BuildAttempt(
        build_job_id=job.id,
        worker_id=worker.id,
        attempt_number=attempt_number,
        status=BuildStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    db.add(attempt)
    worker.state = WorkerState.BUSY
    await db.flush()
    worker.current_build_attempt_id = attempt.id
    await db.flush()
    await db.refresh(attempt)

    releasename = None
    sources = []
    mock_config = None
    target = None
    rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
    release = rel_result.scalar_one_or_none()
    if release:
        releasename = release.releasename
    # Collect sources: prefer this job's package_version so distgit/new-package sources are always included.
    if job.package_version_id:
        src_result = await db.execute(
            select(Source).where(Source.package_version_id == job.package_version_id)
        )
        for src in src_result.scalars().all():
            sources.append({"kind": src.kind, "r2_key": src.r2_key, "url": src.url})
    elif job.package_id:
        from oreon_build.models import PackageVersion as PV

        src_result = await db.execute(
            select(Source)
            .join(PV, Source.package_version_id == PV.id)
            .where(PV.package_id == job.package_id)
        )
        for src in src_result.scalars().all():
            sources.append({"kind": src.kind, "r2_key": src.r2_key, "url": src.url})
    if job.target_id:
        t_result = await db.execute(
            select(BuildTarget).where(BuildTarget.id == job.target_id).options(
                selectinload(BuildTarget.mock_environment)
            )
        )
        target = t_result.scalar_one_or_none()
        if target and target.mock_environment:
            mock_config = target.mock_environment.config_name
    if not mock_config and job.package_id:
        pkg_result = await db.execute(
            select(Package).where(Package.id == job.package_id).options(
                selectinload(Package.preferred_mock_environment)
            )
        )
        pkg = pkg_result.scalar_one_or_none()
        if pkg and pkg.preferred_mock_environment and pkg.preferred_mock_environment.release_id == job.release_id:
            mock_config = pkg.preferred_mock_environment.config_name
    if not mock_config and release:
        fallback = await db.execute(
            select(MockEnvironment).where(MockEnvironment.release_id == job.release_id).limit(1)
        )
        me = fallback.scalar_one_or_none()
        if me:
            mock_config = me.config_name

    # At build time, worker uses <config_name>-<arch> (mock env is not arch-specific)
    build_arch = (target.architecture if target else None) or (worker.architecture or "").strip() or "x86_64"
    branch = getattr(job, "branch", None) or "dev"
    if mock_config:
        base = mock_config.strip()
        # Normalize: if stored value already ends with -x86_64 or -aarch64, treat as base
        for suffix in ("-x86_64", "-aarch64"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        mock_config = base + "-" + build_arch

    return {
        "job": {
            "build_job_id": job.id,
            "build_attempt_id": attempt.id,
            "package_id": job.package_id,
            "release_id": job.release_id,
            "target_id": job.target_id,
            "releasename": releasename,
            "branch": branch,
            "architecture": build_arch,
            "sources": sources,
            "mock_config": mock_config,
        },
    }


@router.get("/cancel-check/{attempt_id}")
async def cancel_check(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
):
    """Worker polls this while a build runs; true when the user cancelled the job."""
    result = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    job_result = await db.execute(select(BuildJob).where(BuildJob.id == attempt.build_job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return {"cancel_requested": job.status == BuildStatus.CANCELLED}


def _put_rpm_bytes_to_r2(key: str, raw: bytes) -> None:
    get_r2_client().put_object(key, raw, content_type="application/x-rpm")


@router.post("/upload-rpm/{attempt_id}")
async def upload_worker_rpm(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    file: UploadFile = File(...),
):
    """
    Receive a built RPM from a worker, GPG-sign on the controller, push to R2, delete temp file.
    Workers no longer need signing keys or direct R2 write access for RPM artifacts.
    """
    settings = get_settings()
    max_bytes = settings.max_worker_rpm_upload_mib * 1024 * 1024

    fname = (file.filename or "package.rpm").strip()
    if not fname.lower().endswith(".rpm"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename must end with .rpm",
        )

    result = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")

    job_result = await db.execute(select(BuildJob).where(BuildJob.id == attempt.build_job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status == BuildStatus.CANCELLED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Job cancelled")

    rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
    release = rel_result.scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Release missing")

    arch = "x86_64"
    if job.target_id:
        t_result = await db.execute(select(BuildTarget).where(BuildTarget.id == job.target_id))
        t = t_result.scalar_one_or_none()
        if t and t.architecture:
            arch = t.architecture
    branch = getattr(job, "branch", None) or "dev"

    fd, tmp_path = tempfile.mkstemp(suffix=".rpm")
    os.close(fd)
    path = Path(tmp_path)
    raw: bytes = b""
    signed = False
    digest = ""
    r2_key = ""
    try:
        total = 0
        with open(path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"RPM exceeds max {settings.max_worker_rpm_upload_mib} MiB",
                    )
                out.write(chunk)

        signed = await asyncio.to_thread(sign_rpm, path)
        raw = await asyncio.to_thread(path.read_bytes)
        digest = hashlib.sha256(raw).hexdigest()

        if fname.endswith(".src.rpm"):
            r2_key = src_r2_key(release.releasename, branch, fname)
        else:
            r2_key = repo_rpms_key(release.releasename, branch, arch, fname)

        await asyncio.to_thread(_put_rpm_bytes_to_r2, r2_key, raw)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("upload-rpm failed attempt_id=%s", attempt_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process RPM",
        ) from exc
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    return {
        "filename": fname,
        "r2_key": r2_key,
        "signed": signed,
        "size_bytes": len(raw),
        "checksum_sha256": digest,
    }


@router.post("/result/{attempt_id}")
async def report_result(
    attempt_id: int,
    payload: ResultPayload,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
):
    result = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    job_result = await db.execute(select(BuildJob).where(BuildJob.id == attempt.build_job_id))
    job = job_result.scalar_one()

    # User cancelled: do not mark job success or promote artifacts when the worker finishes later.
    if job.status == BuildStatus.CANCELLED:
        if payload.log_r2_key:
            attempt.log_r2_key = payload.log_r2_key
        if payload.error_message:
            prev = (attempt.error_message or "").strip()
            attempt.error_message = (prev + "\n" if prev else "") + payload.error_message.strip()
        worker.state = WorkerState.IDLE
        worker.current_build_attempt_id = None
        await db.flush()
        await log_audit(
            db,
            action="build.result",
            account_id=None,
            resource_type="build_job",
            resource_id=str(job.id),
            details=f"status=ignored job_cancelled attempt_id={attempt_id}",
            ip_address=None,
            user_agent="worker",
        )
        return {"status": "ok", "job_cancelled": True}

    attempt.status = BuildStatus(payload.status)
    attempt.finished_at = datetime.now(timezone.utc)
    if payload.error_message:
        attempt.error_message = payload.error_message
    if payload.log_r2_key:
        attempt.log_r2_key = payload.log_r2_key
    job.status = BuildStatus(payload.status)
    job.completed_at = attempt.finished_at
    worker.state = WorkerState.IDLE
    worker.current_build_attempt_id = None
    for a in payload.artifacts:
        art = Artifact(
            build_attempt_id=attempt_id,
            kind=a.kind,
            filename=a.filename,
            r2_key=a.r2_key,
            size_bytes=a.size_bytes,
            checksum_sha256=a.checksum_sha256,
            is_signed=bool(getattr(a, "signed", False)),
        )
        db.add(art)
    await db.flush()

    # On success: ensure RPMs are in dev/, then update repodata, create promotion
    # Worker may upload to builds/ or dev/. If builds/: copy to dev/ FIRST, then we delete builds/ later.
    if payload.status == "success" and payload.artifacts:
        pkg_result = await db.execute(select(Package).where(Package.id == job.package_id))
        pkg = pkg_result.scalar_one_or_none()
        rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
        release = rel_result.scalar_one()
        target_result = await db.execute(select(BuildTarget).where(BuildTarget.id == job.target_id))
        target = target_result.scalar_one_or_none()
        arch = target.architecture if target else None
        branch = getattr(job, "branch", "dev") or "dev"
        if release and arch:
            try:
                r2 = get_r2_client()
                releasename = release.releasename
                # Copy any artifacts from builds/ to dev/ (worker may still use builds/)
                for a in payload.artifacts:
                    if a.r2_key and "/builds/" in a.r2_key:
                        data = r2.get_object(a.r2_key)
                        if data and a.filename:
                            if a.filename.endswith(".src.rpm"):
                                dest = src_r2_key(releasename, branch, a.filename)
                            else:
                                dest = repo_rpms_key(releasename, branch, arch, a.filename)
                            r2.put_object(dest, data)
                prefix = repo_r2_prefix(releasename, branch, arch)
                update_repodata_at_prefix(prefix)
            except Exception:
                pass  # log and continue; job is still marked success
            # Copy package SRPM to releasename/branch/src/ (from uploads) if we have one
            if job.package_version_id:
                src_result = await db.execute(
                    select(Source).where(
                        Source.package_version_id == job.package_version_id,
                        Source.r2_key.isnot(None),
                        Source.r2_key != "",
                    ).limit(1)
                )
                src_row = src_result.scalar_one_or_none()
                if src_row and src_row.r2_key:
                    try:
                        r2 = get_r2_client()
                        data = r2.get_object(src_row.r2_key)
                        if data:
                            filename = src_row.r2_key.split("/")[-1]
                            if filename.endswith(".src.rpm"):
                                dest_key = src_r2_key(release.releasename, branch, filename)
                                r2.put_object(dest_key, data)
                    except Exception:
                        pass
        # Create promotion candidate (pending)
        promo = Promotion(
            release_id=job.release_id,
            from_channel="dev",
            to_channel=None,
            status="pending",
            package_id=job.package_id,
            package_name=pkg.name if pkg else None,
            build_job_id=job.id,
        )
        db.add(promo)
        await db.flush()

    # Delete upload R2 objects after build (success or fail); uploads are not reused for retries.
    # ONLY delete keys under /uploads/ - never touch repo path (dev/arch/RPMS, dev/src).
    if job.package_version_id:
        src_list_result = await db.execute(
            select(Source).where(
                Source.package_version_id == job.package_version_id,
                Source.r2_key.isnot(None),
                Source.r2_key != "",
            )
        )
        try:
            r2 = get_r2_client()
            for src in src_list_result.scalars().all():
                if src.r2_key and "/uploads/" in src.r2_key:
                    try:
                        r2.delete_object(src.r2_key)
                    except Exception:
                        pass
                src.r2_key = None
        except Exception:
            pass
        await db.flush()

    # Delete ONLY keys under releasename/builds/{attempt_id}/ - never touch dev/arch or repo path.
    rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
    release = rel_result.scalar_one_or_none()
    if release:
        exact_builds_prefix = f"{release.releasename}/builds/{attempt_id}/"
        art_keys_result = await db.execute(
            select(Artifact.r2_key).where(
                Artifact.build_attempt_id == attempt_id,
                Artifact.r2_key.isnot(None),
                Artifact.r2_key != "",
            )
        )
        try:
            r2 = get_r2_client()
            for (key,) in art_keys_result.all():
                if key and key.startswith(exact_builds_prefix):
                    try:
                        r2.delete_object(key)
                    except Exception:
                        pass
        except Exception:
            pass

    # Emit audit + Discord for build result
    await log_audit(
        db,
        action="build.result",
        account_id=None,
        resource_type="build_job",
        resource_id=str(job.id),
        details=f"status={payload.status} attempt_id={attempt_id}",
        ip_address=None,
        user_agent="worker",
    )

    return {"status": "ok"}


@router.post("/artifact/{attempt_id}")
async def register_artifact(
    attempt_id: int,
    payload: ArtifactPayload,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
):
    result = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    art = Artifact(
        build_attempt_id=attempt_id,
        kind=payload.kind,
        filename=payload.filename,
        r2_key=payload.r2_key,
        size_bytes=payload.size_bytes,
        checksum_sha256=payload.checksum_sha256,
    )
    db.add(art)
    await db.flush()
    return {"id": art.id}
