# Oreon Build Service
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Worker-facing API: poll job, heartbeat, upload RPMs for signing, report result."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import subprocess
import time
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from starlette.background import BackgroundTask
from sqlalchemy import delete, select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oreon_build.api.deps import DbSession
from oreon_build.config import get_settings
from oreon_build.core.machine_arch import sql_arch_match_values
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.services.worker_token_resolve import resolve_worker_for_token
from oreon_build.core.audit import log_audit
from sqlalchemy.orm import selectinload

from oreon_build.models import (
    Artifact,
    BuildAttemptFinalization,
    BuildAttempt,
    BuildJob,
    BuildStatus,
    BuildTarget,
    MockEnvironment,
    PublishTask,
    RpmCompleteLock,
    Package,
    Promotion,
    Release,
    Source,
    Worker,
    WorkerState,
)
from oreon_build.services.repo_publish import run_repo_publish_for_attempt
from oreon_build.services.r2 import get_r2_client, log_r2_key, repo_r2_prefix, repo_rpms_key, src_r2_key
from oreon_build.services.signing import sign_rpm
from oreon_build.services.worker_artifact_access import source_r2_keys_for_attempt
from oreon_build.services.worker_state_sync import sync_worker_state_after_attempt_finished

router = APIRouter(prefix="/worker", tags=["worker"])
logger = logging.getLogger(__name__)

_MAX_BUILD_LOG_BYTES = 64 * 1024 * 1024

#
# RPM finalize uses a DB-backed distributed lock (see `RpmCompleteLock`) so multiple controller
# workers cannot sign/upload the same R2 key at the same time.


def _pesign_material_configured(settings) -> bool:
    nss = Path((settings.pesign_nss_dir or "").strip()).expanduser()
    try:
        resolved = nss.resolve()
    except OSError:
        return False
    if not resolved.is_dir():
        return False
    if (settings.pesign_rules_json or "").strip():
        return True
    if (settings.pesign_certificate_nickname or "").strip():
        return True
    return False


def _worker_declared_max_parallel_attempts(worker: Worker) -> int:
    """
    Capacity check for poll-claim readiness.
    Prefers worker.capabilities JSON (`max_jobs` or `max_parallel_builds`) and
    falls back to worker runtime default (4) when not declared.
    """
    fallback = 4
    raw = (worker.capabilities or "").strip()
    if not raw:
        return fallback
    try:
        obj = json.loads(raw)
    except Exception:
        return fallback
    if not isinstance(obj, dict):
        return fallback
    val = obj.get("max_jobs")
    if val is None:
        val = obj.get("max_parallel_builds")
    try:
        n = int(val)
    except Exception:
        return fallback
    return max(1, min(n, 64))


def _build_pesign_bundle_file(dest: Path, nss_dir: Path, manifest: dict) -> None:
    raw = json.dumps(manifest, indent=2).encode("utf-8")
    with tarfile.open(dest, "w:gz") as tf:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        tf.addfile(info, io.BytesIO(raw))
        tf.add(str(nss_dir.resolve()), arcname="nss")


async def get_worker(
    db: DbSession,
    x_worker_token: Annotated[str | None, Header(alias="X-Worker-Token")] = None,
) -> Worker:
    if not x_worker_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-Worker-Token required")
    w = await resolve_worker_for_token(db, x_worker_token)
    if not w:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token")
    w.last_seen_at = datetime.now(timezone.utc)
    w.last_heartbeat = datetime.now(timezone.utc)
    await db.flush()
    return w


async def _require_worker(
    db: DbSession,
    x_worker_token: Annotated[str | None, Header(alias="X-Worker-Token")] = None,
) -> Worker:
    if not x_worker_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-Worker-Token required")
    w = await resolve_worker_for_token(db, x_worker_token)
    if not w:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token")
    w.last_seen_at = datetime.now(timezone.utc)
    w.last_heartbeat = datetime.now(timezone.utc)
    await db.flush()
    return w


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
    # Serialize poll/claim per worker row so duplicate processes sharing one token cannot
    # over-claim jobs in parallel.
    wr = await db.execute(select(Worker).where(Worker.id == worker.id).with_for_update())
    locked_worker = wr.scalar_one_or_none()
    if locked_worker is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found")

    if locked_worker.state == WorkerState.OFFLINE:
        locked_worker.state = WorkerState.IDLE
        await db.flush()
    if locked_worker.state not in (WorkerState.IDLE, WorkerState.BUSY):
        return {"job": None}
    running_attempts = int(
        (
            await db.execute(
                select(func.count(BuildAttempt.id)).where(
                    BuildAttempt.worker_id == locked_worker.id,
                    BuildAttempt.status == BuildStatus.RUNNING,
                )
            )
        ).scalar()
        or 0
    )
    max_parallel = _worker_declared_max_parallel_attempts(locked_worker)
    # Readiness reconciliation only.
    # Keep BUSY workers eligible to poll so one worker host can run parallel builds.
    # If DB state says BUSY but no RUNNING attempts remain, heal back to IDLE.
    if running_attempts == 0 and locked_worker.state == WorkerState.BUSY:
        locked_worker.state = WorkerState.IDLE
        locked_worker.current_build_attempt_id = None
        await db.flush()
    elif running_attempts >= max_parallel:
        locked_worker.state = WorkerState.BUSY
        if locked_worker.current_build_attempt_id is None:
            newest_running_attempt_id = await db.scalar(
                select(BuildAttempt.id)
                .where(
                    BuildAttempt.worker_id == locked_worker.id,
                    BuildAttempt.status == BuildStatus.RUNNING,
                )
                .order_by(BuildAttempt.id.desc())
                .limit(1)
            )
            locked_worker.current_build_attempt_id = newest_running_attempt_id
        await db.flush()
        return {"job": None}
    elif running_attempts > 0 and locked_worker.current_build_attempt_id is None:
        newest_running_attempt_id = await db.scalar(
            select(BuildAttempt.id)
            .where(
                BuildAttempt.worker_id == locked_worker.id,
                BuildAttempt.status == BuildStatus.RUNNING,
            )
            .order_by(BuildAttempt.id.desc())
            .limit(1)
        )
        locked_worker.state = WorkerState.BUSY
        locked_worker.current_build_attempt_id = newest_running_attempt_id
        await db.flush()
    # Only return jobs whose target architecture matches this worker (or job has no target for backward compat)
    worker_arch = (locked_worker.architecture or "").strip() or None
    q = (
        select(BuildJob)
        .where(BuildJob.status.in_([BuildStatus.PENDING, BuildStatus.QUEUED]))
        .order_by(BuildJob.priority.desc(), BuildJob.created_at.asc())
    )
    if worker_arch:
        arch_vals = sql_arch_match_values(locked_worker.architecture)
        if arch_vals:
            q = q.outerjoin(BuildTarget, BuildJob.target_id == BuildTarget.id).where(
                (BuildJob.target_id.is_(None)) | (BuildTarget.architecture.in_(arch_vals))
            )
    # One transaction must not claim the same job row twice (concurrent polls, duplicate worker
    # processes with the same token, or LB retry). Lock the chosen row until commit.
    q = q.limit(1).with_for_update(of=BuildJob, skip_locked=True)
    result = await db.execute(q)
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
        worker_id=locked_worker.id,
        attempt_number=attempt_number,
        status=BuildStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    db.add(attempt)
    locked_worker.state = WorkerState.BUSY
    await db.flush()
    locked_worker.current_build_attempt_id = attempt.id
    await db.flush()
    await db.refresh(attempt)

    releasename = None
    sources = []
    mock_config = None
    selected_mock_env: MockEnvironment | None = None
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
            selected_mock_env = target.mock_environment
            mock_config = selected_mock_env.config_name
    if not mock_config and job.package_id:
        pkg_result = await db.execute(
            select(Package).where(Package.id == job.package_id).options(
                selectinload(Package.preferred_mock_environment)
            )
        )
        pkg = pkg_result.scalar_one_or_none()
        if pkg and pkg.preferred_mock_environment and pkg.preferred_mock_environment.release_id == job.release_id:
            selected_mock_env = pkg.preferred_mock_environment
            mock_config = selected_mock_env.config_name
    if not mock_config and release:
        fallback = await db.execute(
            select(MockEnvironment).where(MockEnvironment.release_id == job.release_id).limit(1)
        )
        me = fallback.scalar_one_or_none()
        if me:
            selected_mock_env = me
            mock_config = me.config_name

    # At build time, worker uses <config_name>-<arch> (mock env is not arch-specific)
    build_arch = (target.architecture if target else None) or (worker.architecture or "").strip() or "x86_64"
    branch = r2_publish_branch(release)
    if mock_config:
        base = mock_config.strip()
        # Normalize: if stored value already ends with -x86_64 or -aarch64, treat as base
        for suffix in ("-x86_64", "-aarch64"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        mock_config = base + "-" + build_arch

    mock_additional_repos: list[str] = []
    if selected_mock_env and selected_mock_env.additional_repo_urls:
        mock_additional_repos = [str(u).strip() for u in selected_mock_env.additional_repo_urls if str(u).strip()]

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
            "mock_additional_repos": mock_additional_repos,
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
    # Job may already be PENDING again after cancel+retry while this attempt row is still
    # CANCELLED; workers must stop mock based on the attempt, not only job.status.
    cancel_requested = job.status == BuildStatus.CANCELLED or attempt.status == BuildStatus.CANCELLED
    return {"cancel_requested": cancel_requested}


def _put_rpm_bytes_to_r2(key: str, raw: bytes) -> None:
    get_r2_client().put_object(key, raw, content_type="application/x-rpm")


_RPM_UPLOAD_CONTENT_TYPE = "application/x-rpm"

_RPM_QUIET_TIMEOUT_SEC = 600


def _rpm_subproc_timeout_sec(path: Path) -> int:
    """
    Large SRPMs (e.g. linux-firmware) need a longer ``rpm -qp`` window or nevra / sig probes time out and
    idempotent complete retries falsely fail with HTTP 400.
    """
    try:
        mib = max(1, path.stat().st_size // (1024 * 1024))
    except OSError:
        return _RPM_QUIET_TIMEOUT_SEC
    return min(7200, max(_RPM_QUIET_TIMEOUT_SEC, mib * 3))


def _rpm_nevra_stem_from_path(path: Path) -> str | None:
    """NEVRA string like NAME-VERSION-RELEASE.ARCH (no trailing .rpm)."""
    to = _rpm_subproc_timeout_sec(path)
    try:
        r = subprocess.run(
            ["rpm", "-qp", "--qf", "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}", str(path)],
            capture_output=True,
            text=True,
            timeout=to,
            check=False,
        )
        if r.returncode != 0:
            return None
        s = (r.stdout or "").strip()
        return s or None
    except Exception:
        return None


def _rpm_signature_verifies(path: Path) -> bool:
    try:
        r = subprocess.run(
            ["rpm", "-K", str(path)],
            capture_output=True,
            text=True,
            timeout=_rpm_subproc_timeout_sec(path),
            check=False,
        )
        return r.returncode == 0
    except Exception:
        return False


def _rpm_appears_gpg_signed(path: Path) -> bool:
    """
    Check signature presence without verifying against the local rpmdb keys.

    rpm -K can fail when the public key isn't imported into the controller's rpmdb.
    Some builds only populate OPENPGP/SIGGPG style slots in the header, so SIGPGP alone is not enough.
    """
    to = _rpm_subproc_timeout_sec(path)
    for qf in (
        "%{SIGPGP:pgpsig}",
        "%{SIGGPG:armor}",
        "%{OPENPGP:armor}",
    ):
        try:
            r = subprocess.run(
                ["rpm", "-qp", "--qf", qf, str(path)],
                capture_output=True,
                text=True,
                timeout=to,
                check=False,
            )
            if r.returncode == 0 and (r.stdout or "").strip():
                return True
        except Exception:
            continue
    return False


def _presigned_rpm_already_finalized_on_retry(path: Path, fname: str) -> bool:
    """
    After a successful complete, R2 holds the GPG-signed RPM while the worker may retry complete
    with the original unsigned size/checksum (e.g. read timeout on the JSON response). Accept the
    object if it is the same NEVRA as the filename and signatures verify.
    """
    name = Path(fname.strip()).name
    if not name.lower().endswith(".rpm"):
        return False
    stem = Path(name).stem
    got = _rpm_nevra_stem_from_path(path)
    if not got:
        return False
    if got != stem:
        # Some rpm builds include an epoch prefix "(none):" or "0:" in tag output.
        ep = subprocess.run(
            ["rpm", "-qp", "--qf", "%{EPOCH}:%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}", str(path)],
            capture_output=True,
            text=True,
            timeout=_rpm_subproc_timeout_sec(path),
            check=False,
        )
        if ep.returncode == 0:
            alt = (ep.stdout or "").strip()
            if alt.startswith("(none):") or alt.startswith("0:"):
                rest = alt.split(":", 1)[-1]
                if rest == stem:
                    # Don't require rpmdb key verification; just check signature presence.
                    return _rpm_appears_gpg_signed(path)
        return False

    # Don't require rpm -K verification for idempotent retry. Just verify that the object looks
    # like the expected NEVRA and has a PGP signature already embedded.
    return _rpm_appears_gpg_signed(path)


async def _resolve_worker_rpm_upload_context(
    db: DbSession,
    worker: Worker,
    attempt_id: int,
    fname_raw: str,
) -> tuple[BuildAttempt, BuildJob, Release, str, str, str]:
    """
    Validate worker/attempt/job and compute repo ``r2_key`` for ``fname``.
    Returns ``attempt``, ``job``, ``release``, ``arch``, ``branch``, ``r2_key``.
    """
    fname = Path(fname_raw.strip()).name
    if not fname or not fname.lower().endswith(".rpm"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename must end with .rpm",
        )

    ar = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = ar.scalar_one_or_none()
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Release missing",
        )

    arch = "x86_64"
    if job.target_id:
        t_result = await db.execute(select(BuildTarget).where(BuildTarget.id == job.target_id))
        t = t_result.scalar_one_or_none()
        if t and t.architecture:
            arch = t.architecture
    branch = r2_publish_branch(release)

    if fname.endswith(".src.rpm"):
        r2_key = src_r2_key(release.releasename, branch, fname)
    else:
        r2_key = repo_rpms_key(release.releasename, branch, arch, fname)

    return attempt, job, release, arch, branch, r2_key


class WorkerRpmPresignBody(BaseModel):
    filename: str


class WorkerRpmCompleteBody(BaseModel):
    filename: str
    checksum_sha256: str
    size_bytes: int


@router.post("/upload-rpm/presign/{attempt_id}")
async def upload_worker_rpm_presign(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    body: WorkerRpmPresignBody,
):
    """
    Presigned PUT to R2 so workers can upload very large RPMs without POSTing through Cloudflare Tunnel
    (which often returns 413 for very large bodies). Binary RPMs can still use multipart ``/upload-rpm/``.
    """
    settings = get_settings()
    _, _, _, _, _, r2_key = await _resolve_worker_rpm_upload_context(
        db, worker, attempt_id, body.filename
    )
    expires = settings.worker_rpm_presign_put_expires_seconds
    url = get_r2_client().generate_presigned_put_url(
        r2_key,
        content_type=_RPM_UPLOAD_CONTENT_TYPE,
        expires_in=expires,
    )
    await log_audit(
        db,
        action="worker.rpm_presign",
        resource_type="build_attempt",
        resource_id=str(attempt_id),
        details=f"worker_id={worker.id} key={r2_key}",
        emit_discord=False,
    )
    return {
        "put_url": url,
        "headers": {"Content-Type": _RPM_UPLOAD_CONTENT_TYPE},
        "expires_in": expires,
    }


@router.post("/upload-rpm/complete/{attempt_id}")
async def upload_worker_rpm_complete(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    body: WorkerRpmCompleteBody,
):
    """
    After the worker PUTs the unsigned RPM to R2 using a presigned URL, this runs on the controller:
    HEAD+GET from R2 into a temp file (same network path as any S3 client, not through the worker
    tunnel), verify size and SHA-256, ``rpm --addsign`` on that file, then ``put_object`` overwrites
    the same R2 key with the signed RPM bytes.
    """
    settings = get_settings()
    max_bytes = settings.max_worker_rpm_upload_mib * 1024 * 1024
    if body.size_bytes > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"RPM exceeds max {settings.max_worker_rpm_upload_mib} MiB",
        )

    _, _, _, _, _, r2_key = await _resolve_worker_rpm_upload_context(
        db, worker, attempt_id, body.filename
    )
    want_sha = (body.checksum_sha256 or "").strip().lower()
    if len(want_sha) != 64 or any(c not in "0123456789abcdef" for c in want_sha):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid checksum_sha256")
    sz = int(body.size_bytes)

    verify_retries = int((os.environ.get("OREON_RPM_COMPLETE_VERIFY_RETRIES") or "8").strip())
    verify_retries = max(1, min(verify_retries, 24))
    head_polls = int((os.environ.get("OREON_RPM_COMPLETE_HEAD_POLLS") or "48").strip())
    head_polls = max(8, min(head_polls, 160))

    def _finalize() -> tuple[bool, str, int, str]:
        fname = Path(body.filename.strip()).name

        def _one_attempt() -> tuple[bool, str, int, str]:
            fd, tmp_path = tempfile.mkstemp(suffix=".rpm")
            os.close(fd)
            path = Path(tmp_path)
            try:
                r2 = get_r2_client()
                cl: int | None = None
                for hi in range(head_polls):
                    cl = r2.head_object_content_length(r2_key)
                    if cl is not None:
                        break
                    time.sleep(min(0.25 * (hi + 1), 2.5))
                if cl is None:
                    raise FileNotFoundError("RPM not in R2 yet (complete presigned PUT first)")
                if cl > max_bytes:
                    raise ValueError(
                        f"R2 object exceeds max {settings.max_worker_rpm_upload_mib} MiB ({cl} bytes)"
                    )

                stream = r2.get_object_stream(r2_key)
                if stream is None:
                    raise FileNotFoundError("RPM not in R2")
                h = hashlib.sha256()
                try:
                    with open(path, "wb") as out:
                        total = 0
                        while True:
                            chunk = stream.read(8 * 1024 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError(
                                    f"RPM exceeds max {settings.max_worker_rpm_upload_mib} MiB"
                                )
                            h.update(chunk)
                            out.write(chunk)
                finally:
                    stream.close()

                actual_size = path.stat().st_size
                actual_sha = h.hexdigest()

                if actual_size == sz and actual_sha == want_sha:
                    signed = sign_rpm(path)
                    signed_size = path.stat().st_size
                    h2 = hashlib.sha256()
                    with open(path, "rb") as fh:
                        while True:
                            chunk = fh.read(8 * 1024 * 1024)
                            if not chunk:
                                break
                            h2.update(chunk)
                    signed_sha = h2.hexdigest()
                    with open(path, "rb") as fh2:
                        r2.upload_fileobj(r2_key, fh2, content_type=_RPM_UPLOAD_CONTENT_TYPE)
                    return signed, r2_key, signed_size, signed_sha

                if _presigned_rpm_already_finalized_on_retry(path, fname):
                    logger.warning(
                        "presigned RPM complete idempotent retry ok for %s attempt_id=%s",
                        fname,
                        attempt_id,
                    )
                    return True, r2_key, actual_size, actual_sha

                raise ValueError(
                    "Upload verify failed (R2 size=%s sha256=%s; worker size=%s sha256=%s). "
                    "Retry after a completed sign changes the object on disk."
                    % (actual_size, actual_sha[:16], sz, want_sha[:16])
                )
            finally:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

        for round_i in range(verify_retries):
            try:
                return _one_attempt()
            except FileNotFoundError as e:
                if round_i + 1 < verify_retries:
                    delay = min(3.0 * (1.45**round_i), 60.0)
                    logger.warning(
                        "rpm finalize R2 not ready key=%s attempt_id=%s round=%s/%s sleep=%.1fs: %s",
                        r2_key,
                        attempt_id,
                        round_i + 1,
                        verify_retries,
                        delay,
                        e,
                    )
                    time.sleep(delay)
                    continue
                raise
            except ValueError as e:
                msg_low = str(e).lower()
                if "exceeds max" in msg_low:
                    raise
                if round_i + 1 < verify_retries and "upload verify failed" in msg_low:
                    delay = min(4.0 * (1.45**round_i), 90.0)
                    logger.warning(
                        "rpm finalize verify retry key=%s attempt_id=%s round=%s/%s sleep=%.1fs: %s",
                        r2_key,
                        attempt_id,
                        round_i + 1,
                        verify_retries,
                        delay,
                        e,
                    )
                    time.sleep(delay)
                    continue
                raise
        raise RuntimeError("rpm finalize fell through without result")

    from datetime import timedelta
    from uuid import uuid4

    lock_ttl_sec = int((os.environ.get("OREON_WORKER_RPM_COMPLETE_LOCK_TTL_SEC") or "43200").strip())
    max_wait_sec = int((os.environ.get("OREON_WORKER_RPM_COMPLETE_LOCK_MAX_WAIT_SEC") or "3600").strip())
    owner = str(uuid4())

    lock_acquired = False
    try:
        start = datetime.now(timezone.utc)
        while True:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=lock_ttl_sec)
            stmt = (
                pg_insert(RpmCompleteLock.__table__)
                .values(
                    attempt_id=attempt_id,
                    r2_key=r2_key,
                    owner=owner,
                    expires_at=expires_at,
                )
                .on_conflict_do_nothing(index_elements=["attempt_id", "r2_key"])
                .returning(RpmCompleteLock.owner)
            )
            got = await db.execute(stmt)
            lock_owner = got.scalar_one_or_none()
            if lock_owner is not None:
                await db.commit()
                lock_acquired = True
                break

            now = datetime.now(timezone.utc)
            existing_res = await db.execute(
                select(RpmCompleteLock).where(
                    RpmCompleteLock.attempt_id == attempt_id, RpmCompleteLock.r2_key == r2_key
                )
            )
            existing = existing_res.scalar_one_or_none()
            if existing is not None and existing.expires_at <= now:
                await db.execute(
                    delete(RpmCompleteLock).where(
                        RpmCompleteLock.attempt_id == attempt_id, RpmCompleteLock.r2_key == r2_key
                    )
                )
                await db.commit()
                continue

            if (now - start).total_seconds() > max_wait_sec:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Timed out waiting for RPM finalize lock",
                )
            await asyncio.sleep(2)

        try:
            signed, rk, signed_size, signed_sha = await asyncio.to_thread(_finalize)
        except FileNotFoundError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    finally:
        if lock_acquired:
            try:
                await db.execute(
                    delete(RpmCompleteLock).where(
                        RpmCompleteLock.attempt_id == attempt_id,
                        RpmCompleteLock.r2_key == r2_key,
                        RpmCompleteLock.owner == owner,
                    )
                )
                await db.commit()
            except Exception:
                pass

    digest = signed_sha
    fname = Path(body.filename.strip()).name
    await log_audit(
        db,
        action="worker.rpm_upload_presigned_complete",
        resource_type="build_attempt",
        resource_id=str(attempt_id),
        details=f"worker_id={worker.id} {fname} {rk}",
        emit_discord=False,
    )
    return {
        "filename": fname,
        "r2_key": rk,
        "signed": signed,
        "pesign_applied": False,
        "size_bytes": signed_size,
        "checksum_sha256": digest,
    }


def _rpm_name_from_bytes(data: bytes) -> str | None:
    """Extract RPM NAME from bytes using host rpm command."""
    if not data:
        return None
    path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".rpm", delete=False) as f:
            f.write(data)
            path = f.name
        r = subprocess.run(
            ["rpm", "-qp", "--qf", "%{NAME}", path],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if r.returncode != 0:
            return None
        name = (r.stdout or "").strip()
        return name or None
    except Exception:
        return None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _prune_superseded_repo_objects(
    *,
    releasename: str,
    branch: str,
    arch: str,
    keep_artifact_filenames: set[str],
    payload_artifacts: list[ArtifactPayload],
) -> None:
    """
    Remove older RPM/SRPM objects in the same branch when a new release is published,
    including skipped-release upgrades (e.g. -11 -> -14).
    """
    r2 = get_r2_client()

    # Resolve package NAMEs from newly produced payload artifacts.
    binary_names: set[str] = set()
    src_names: set[str] = set()
    for a in payload_artifacts:
        fn = (a.filename or "").strip()
        if not fn.endswith(".rpm"):
            continue
        key = src_r2_key(releasename, branch, fn) if fn.endswith(".src.rpm") else repo_rpms_key(releasename, branch, arch, fn)
        data = r2.get_object(key)
        name = _rpm_name_from_bytes(data) if data else None
        if not name:
            continue
        if fn.endswith(".src.rpm"):
            src_names.add(name)
        else:
            binary_names.add(name)

    # Binary RPMs (same branch/arch)
    bin_prefix = f"{repo_r2_prefix(releasename, branch, arch)}/RPMS/"
    for obj in r2.list_objects(bin_prefix):
        key = obj.get("key")
        if not key:
            continue
        base = key.rsplit("/", 1)[-1]
        if not base.endswith(".rpm") or base.endswith(".src.rpm"):
            continue
        if base in keep_artifact_filenames:
            continue
        if not any(base.startswith(n + "-") for n in binary_names):
            continue
        data = r2.get_object(key)
        old_name = _rpm_name_from_bytes(data) if data else None
        if old_name and old_name in binary_names:
            r2.delete_object(key)
            logger.info("Pruned superseded binary RPM %s", key)

    # Source RPMs (same branch/src)
    src_prefix = f"{releasename}/{branch}/src/"
    for obj in r2.list_objects(src_prefix):
        key = obj.get("key")
        if not key:
            continue
        base = key.rsplit("/", 1)[-1]
        if not base.endswith(".src.rpm"):
            continue
        if base in keep_artifact_filenames:
            continue
        if not any(base.startswith(n + "-") for n in src_names):
            continue
        data = r2.get_object(key)
        old_name = _rpm_name_from_bytes(data) if data else None
        if old_name and old_name in src_names:
            r2.delete_object(key)
            logger.info("Pruned superseded SRPM %s", key)


_BUILD_LOG_PUT_CONTENT_TYPE = "text/plain; charset=utf-8"


async def _resolve_worker_build_log_r2_key(
    db: DbSession,
    worker: Worker,
    attempt_id: int,
) -> str:
    ar = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = ar.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")

    job_result = await db.execute(select(BuildJob).where(BuildJob.id == attempt.build_job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
    release = rel_result.scalar_one_or_none()
    if not release:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Release missing",
        )
    return log_r2_key(release.releasename, attempt_id)


async def _assign_attempt_build_log_key(db: DbSession, worker: Worker, attempt_id: int, key: str) -> None:
    """Expose build logs in the UI while the job is still running (not only after result POST)."""
    r = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    att = r.scalar_one_or_none()
    if att is None:
        return
    att.log_r2_key = key
    await db.flush()


class WorkerBuildLogCompleteBody(BaseModel):
    checksum_sha256: str
    size_bytes: int


@router.post("/build-log/presign/{attempt_id}")
async def worker_build_log_presign(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
):
    """
    Presigned PUT so workers can stream large logs to R2 without tunneling huge bodies through
    Cloudflare (which often returns 524 while the origin buffers and uploads to R2).
    """
    settings = get_settings()
    key = await _resolve_worker_build_log_r2_key(db, worker, attempt_id)
    expires = settings.worker_rpm_presign_put_expires_seconds
    url = get_r2_client().generate_presigned_put_url(
        key,
        content_type=_BUILD_LOG_PUT_CONTENT_TYPE,
        expires_in=expires,
    )
    await log_audit(
        db,
        action="worker.build_log_presign",
        resource_type="build_attempt",
        resource_id=str(attempt_id),
        details=f"worker_id={worker.id} key={key}",
        emit_discord=False,
    )
    return {
        "put_url": url,
        "headers": {"Content-Type": _BUILD_LOG_PUT_CONTENT_TYPE},
        "expires_in": expires,
    }


@router.post("/build-log/complete/{attempt_id}")
async def worker_build_log_complete(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    body: WorkerBuildLogCompleteBody,
):
    """After presigned PUT, verify log object in R2 (size + SHA-256). No re-upload; log is not GPG-signed."""
    if body.size_bytes > _MAX_BUILD_LOG_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Log exceeds max {_MAX_BUILD_LOG_BYTES // (1024 * 1024)} MiB",
        )
    key = await _resolve_worker_build_log_r2_key(db, worker, attempt_id)
    want_sha = (body.checksum_sha256 or "").strip().lower()
    if len(want_sha) != 64 or any(c not in "0123456789abcdef" for c in want_sha):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid checksum_sha256")
    sz = int(body.size_bytes)

    def _verify() -> str:
        r2 = get_r2_client()
        cl = r2.head_object_content_length(key)
        if cl is None:
            raise FileNotFoundError("Build log not in R2 yet (complete presigned PUT first)")
        if cl != sz:
            raise ValueError(f"Size mismatch: R2 has {cl} bytes, worker claimed {sz}")

        # Fast retry path: if worker uploaded metadata in the presigned PUT, the controller can
        # validate integrity via HEAD user-metadata without re-hashing the whole object.
        meta = r2.head_object_metadata(key)
        if meta:
            meta_sha = (meta.get("sha256") or meta.get("checksum_sha256") or "").strip().lower()
            meta_sz_raw = (meta.get("size_bytes") or meta.get("size") or "").strip()
            meta_ok = meta_sha == want_sha and meta_sz_raw.isdigit() and int(meta_sz_raw) == sz
            if meta_ok:
                return key

        stream = r2.get_object_stream(key)
        if stream is None:
            raise FileNotFoundError("Build log not in R2")
        h = hashlib.sha256()
        try:
            while True:
                chunk = stream.read(8 * 1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        finally:
            stream.close()
        if h.hexdigest() != want_sha:
            raise ValueError("SHA256 of uploaded log does not match checksum_sha256")
        return key

    try:
        await asyncio.to_thread(_verify)
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    await _assign_attempt_build_log_key(db, worker, attempt_id, key)

    await log_audit(
        db,
        action="worker.build_log_presign_complete",
        resource_type="build_attempt",
        resource_id=str(attempt_id),
        details=f"worker_id={worker.id} {key}",
        emit_discord=False,
    )
    return {"log_r2_key": key}


@router.put("/build-log/{attempt_id}")
async def worker_upload_build_log(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    content: bytes = Body(...),
):
    """
    Store a build log in R2 on behalf of the authenticated worker. No R2 credentials on the worker.
    Large logs should use ``/build-log/presign`` + PUT to R2 + ``/build-log/complete`` from the worker instead.
    """
    if len(content) > _MAX_BUILD_LOG_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Log exceeds max {_MAX_BUILD_LOG_BYTES // (1024 * 1024)} MiB",
        )
    key = await _resolve_worker_build_log_r2_key(db, worker, attempt_id)

    def _put() -> None:
        get_r2_client().put_object(key, content, content_type=_BUILD_LOG_PUT_CONTENT_TYPE)

    await asyncio.to_thread(_put)
    await _assign_attempt_build_log_key(db, worker, attempt_id, key)
    return {"log_r2_key": key}


@router.get("/source-blob/{attempt_id}")
async def worker_download_source_blob(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    key: str = Query(..., min_length=1, max_length=2048),
):
    """Stream an object from R2 for keys attached to this job. Workers need no R2 creds."""
    ar = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    if not ar.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    allowed = await source_r2_keys_for_attempt(db, attempt_id)
    if key not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Object key not allowed for this attempt",
        )

    def _get() -> bytes | None:
        return get_r2_client().get_object(key)

    data = await asyncio.to_thread(_get)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
    return Response(content=data, media_type="application/octet-stream")


@router.get("/pesign-bundle/{attempt_id}")
async def worker_download_pesign_bundle(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
):
    """
    Tar.gz of NSS DB plus manifest. Only while attempt is RUNNING; only the assigned worker; use TLS.
    """
    settings = get_settings()
    ar = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.id == attempt_id,
            BuildAttempt.worker_id == worker.id,
        )
    )
    attempt = ar.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    if attempt.status != BuildStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Pesign bundle only while attempt is running",
        )
    if not _pesign_material_configured(settings):
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    nss_dir = Path(settings.pesign_nss_dir).expanduser().resolve()
    manifest = {
        "version": 1,
        "pesign_rules_json": settings.pesign_rules_json,
        "pesign_certificate_nickname": settings.pesign_certificate_nickname,
        "pesign_package_name_regex": settings.pesign_package_name_regex,
        "pesign_token": settings.pesign_token,
        "pesign_strict": settings.pesign_strict,
        "pesign_aarch64_qemu_prefix": settings.pesign_aarch64_qemu_prefix,
        "pesign_aarch64_guest_shell": settings.pesign_aarch64_guest_shell,
        "nss_subdir": "nss",
    }
    fd, tmp = tempfile.mkstemp(suffix=".tar.gz")
    os.close(fd)
    out_path = Path(tmp)

    def _build() -> None:
        _build_pesign_bundle_file(out_path, nss_dir, manifest)

    try:
        await asyncio.to_thread(_build)
    except Exception:
        out_path.unlink(missing_ok=True)
        raise

    await log_audit(
        db,
        action="worker.pesign_bundle_download",
        resource_type="build_attempt",
        resource_id=str(attempt_id),
        details=f"worker_id={worker.id}",
        emit_discord=False,
    )

    out_str = str(out_path)

    def _cleanup() -> None:
        try:
            os.unlink(out_str)
        except OSError:
            pass

    return FileResponse(
        path=out_str,
        filename=f"pesign-bundle-{attempt_id}.tar.gz",
        media_type="application/gzip",
        background=BackgroundTask(_cleanup),
    )


@router.post("/upload-rpm/{attempt_id}")
async def upload_worker_rpm(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    file: UploadFile = File(...),
):
    """
    Receive a built RPM. Pesign runs on the worker (bundle from GET .../pesign-bundle). GPG here.
    """
    settings = get_settings()
    max_bytes = settings.max_worker_rpm_upload_mib * 1024 * 1024

    fname_raw = file.filename or "package.rpm"
    _, _, _, _, _, r2_key = await _resolve_worker_rpm_upload_context(
        db, worker, attempt_id, fname_raw
    )
    fname = Path(fname_raw.strip()).name

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
        "pesign_applied": False,
        "size_bytes": len(raw),
        "checksum_sha256": digest,
    }


@router.post("/mock-complete/{attempt_id}")
async def worker_mock_complete(
    attempt_id: int,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
):
    """Worker calls this after ``mock --rebuild`` succeeds so the UI can show success while RPMs upload and the controller signs/publishes."""
    result = await db.execute(
        select(BuildAttempt)
        .where(BuildAttempt.id == attempt_id, BuildAttempt.worker_id == worker.id)
        .with_for_update()
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    if attempt.status != BuildStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Attempt is not running",
        )
    attempt.mock_succeeded_at = datetime.now(timezone.utc)
    attempt.signing_status = "running"
    await db.flush()
    return {"status": "ok"}


@router.post("/result/{attempt_id}")
async def report_result(
    attempt_id: int,
    payload: ResultPayload,
    db: DbSession,
    worker: Annotated[Worker, Depends(_require_worker)],
    background_tasks: BackgroundTasks,
):
    result = await db.execute(
        select(BuildAttempt)
        .where(BuildAttempt.id == attempt_id, BuildAttempt.worker_id == worker.id)
        .with_for_update()
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    job_result = await db.execute(select(BuildJob).where(BuildJob.id == attempt.build_job_id))
    job = job_result.scalar_one()

    # Idempotency: if we already finalized this attempt, do nothing (no artifacts insert,
    # no promotion duplication, no repodata/prune/repack side effects).
    existing_final = await db.execute(
        select(BuildAttemptFinalization).where(BuildAttemptFinalization.attempt_id == attempt_id)
    )
    if existing_final.scalar_one_or_none() is not None:
        return {"status": "ok", "already_finalized": True}

    now = datetime.now(timezone.utc)

    async def _insert_finalization_row(finalized_status: str) -> None:
        stmt = (
            pg_insert(BuildAttemptFinalization.__table__)
            .values(
                attempt_id=attempt_id,
                finalized_status=finalized_status,
                log_r2_key=payload.log_r2_key or attempt.log_r2_key,
                error_message=(
                    payload.error_message if payload.error_message else attempt.error_message
                ),
                finalized_at=now,
            )
            .on_conflict_do_nothing(index_elements=["attempt_id"])
        )
        await db.execute(stmt)
        await db.flush()

    async def _enqueue_repo_publish_task() -> None:
        stmt = (
            pg_insert(PublishTask.__table__)
            .values(kind="repo_publish", attempt_id=attempt_id, build_job_id=job.id)
            .on_conflict_do_nothing(index_elements=["attempt_id"])
        )
        await db.execute(stmt)

    # User cancelled: do not mark job success or promote artifacts when the worker finishes later.
    if job.status == BuildStatus.CANCELLED:
        if payload.log_r2_key:
            attempt.log_r2_key = payload.log_r2_key
        if payload.error_message:
            prev = (attempt.error_message or "").strip()
            attempt.error_message = (prev + "\n" if prev else "") + payload.error_message.strip()
        if attempt.status == BuildStatus.RUNNING:
            attempt.status = BuildStatus.CANCELLED
            attempt.finished_at = datetime.now(timezone.utc)
        attempt.signing_status = "failed"
        await db.flush()
        await sync_worker_state_after_attempt_finished(db, worker)
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
        await _insert_finalization_row(attempt.status.value)
        return {"status": "ok", "job_cancelled": True}

    # Worker default HTTP read timeout can fire after this handler already committed rows. A later
    # orphan "failed" POST must not wipe success, and must fix failed status when artifacts exist.
    if str(payload.status).lower() == "failed":
        prior_art_count = await db.scalar(
            select(func.count(Artifact.id)).where(Artifact.build_attempt_id == attempt_id)
        )
        if (prior_art_count or 0) > 0:
            if attempt.status == BuildStatus.SUCCESS:
                logger.warning(
                    "Ignoring stale failed worker report for attempt %s (success + %d artifacts)",
                    attempt_id,
                    prior_art_count,
                )
                return {"status": "ok", "ignored_stale_failure": True}
            if attempt.status == BuildStatus.FAILED:
                logger.warning(
                    "Repairing attempt %s: failed report but %d artifacts (timeout race); restoring success",
                    attempt_id,
                    prior_art_count,
                )
                attempt.status = BuildStatus.SUCCESS
                attempt.error_message = None
                attempt.finished_at = datetime.now(timezone.utc)
                attempt.signing_status = "signed"
                job.status = BuildStatus.SUCCESS
                job.completed_at = attempt.finished_at
                if payload.log_r2_key:
                    attempt.log_r2_key = payload.log_r2_key
                await db.flush()
                await sync_worker_state_after_attempt_finished(db, worker)
                # Durably persist the repaired terminal status before follow-up side effects.
                # If any later step fails, worker retry should continue from SUCCESS, not RUNNING.
                await db.commit()
                promo_count = await db.scalar(
                    select(func.count(Promotion.id)).where(Promotion.build_job_id == job.id)
                )
                if (promo_count or 0) == 0:
                    pkg_result = await db.execute(select(Package).where(Package.id == job.package_id))
                    pkg = pkg_result.scalar_one_or_none()
                    db.add(
                        Promotion(
                            release_id=job.release_id,
                            from_channel="dev",
                            to_channel=None,
                            status="pending",
                            package_id=job.package_id,
                            package_name=pkg.name if pkg else None,
                            build_job_id=job.id,
                        )
                    )
                    await db.flush()
                rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
                release = rel_result.scalar_one_or_none()
                target_result = await db.execute(select(BuildTarget).where(BuildTarget.id == job.target_id))
                target = target_result.scalar_one_or_none()
                arch = target.architecture if target else None
                branch = r2_publish_branch(release)
                job.branch = branch
                if release and arch:
                    await _enqueue_repo_publish_task()
                    background_tasks.add_task(run_repo_publish_for_attempt, attempt_id)

                await log_audit(
                    db,
                    action="build.result",
                    account_id=None,
                    resource_type="build_job",
                    resource_id=str(job.id),
                    details=f"status=repaired_success attempt_id={attempt_id}",
                    ip_address=None,
                    user_agent="worker",
                )
                await _insert_finalization_row(attempt.status.value)
                return {"status": "ok", "repaired_stale_failure": True}

    attempt.status = BuildStatus(payload.status)
    attempt.finished_at = datetime.now(timezone.utc)
    st_l = str(payload.status).lower()
    if st_l == "success":
        attempt.signing_status = "signed"
    elif st_l in ("failed", "cancelled"):
        attempt.signing_status = "failed"
    if payload.error_message:
        attempt.error_message = payload.error_message
    if payload.log_r2_key:
        attempt.log_r2_key = payload.log_r2_key
    job.status = BuildStatus(payload.status)
    job.completed_at = attempt.finished_at
    await db.flush()
    await sync_worker_state_after_attempt_finished(db, worker)
    # Critical durability boundary.
    # Final status transitions must survive failures in artifact insert / repo publish enqueue /
    # promotion creation / R2 cleanup during large autobuild bursts.
    await db.commit()
    for a in payload.artifacts:
        stmt = (
            pg_insert(Artifact.__table__)
            .values(
                build_attempt_id=attempt_id,
                kind=a.kind,
                filename=a.filename,
                r2_key=a.r2_key,
                size_bytes=a.size_bytes,
                checksum_sha256=a.checksum_sha256,
                is_signed=bool(getattr(a, "signed", False)),
            )
            .on_conflict_do_nothing(
                index_elements=["build_attempt_id", "kind", "filename", "r2_key"]
            )
        )
        await db.execute(stmt)
    # Persist artifact rows immediately. The job/attempt success status was already committed
    # above, so a later side-effect failure must not erase artifact metadata from the UI/API.
    await db.commit()

    # On success: ensure RPMs are in dev/, then update repodata, create promotion
    # Worker may upload to builds/ or dev/. If builds/: copy to dev/ FIRST, then we delete builds/ later.
    if str(payload.status).lower() == "success":
        try:
            pkg_result = await db.execute(select(Package).where(Package.id == job.package_id))
            pkg = pkg_result.scalar_one_or_none()
            rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
            release = rel_result.scalar_one()
            target_result = await db.execute(select(BuildTarget).where(BuildTarget.id == job.target_id))
            target = target_result.scalar_one_or_none()
            arch = target.architecture if target else None
            branch = r2_publish_branch(release)
            job.branch = branch
            if release and arch:
                # Durable row for orphan recovery; immediate run via BackgroundTasks (same work, lock dedupes).
                await _enqueue_repo_publish_task()
                background_tasks.add_task(run_repo_publish_for_attempt, attempt_id)
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
            # Create promotion candidate (pending), but don't duplicate it on retries.
            promo_count = await db.scalar(
                select(func.count(Promotion.id)).where(
                    Promotion.build_job_id == job.id,
                    Promotion.status == "pending",
                    Promotion.to_channel.is_(None),
                )
            )
            if (promo_count or 0) == 0:
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
            await db.commit()
        except Exception:
            logger.exception(
                "post-result success side effects failed for attempt_id=%s job_id=%s",
                attempt_id,
                job.id,
            )
            await db.rollback()

    # Delete upload R2 objects after build (success or fail); uploads are not reused for retries.
    # ONLY delete keys under /uploads/ - never touch repo path (dev/arch/RPMS, dev/src).
    if job.package_version_id:
        try:
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
            await db.commit()
        except Exception:
            logger.exception("upload-source cleanup failed for attempt_id=%s", attempt_id)
            await db.rollback()

    # Delete ONLY keys under releasename/builds/{attempt_id}/ - never touch dev/arch or repo path.
    rel_result = await db.execute(select(Release).where(Release.id == job.release_id))
    release = rel_result.scalar_one_or_none()
    if release and attempt.status != BuildStatus.SUCCESS:
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
    await _insert_finalization_row(attempt.status.value)
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
