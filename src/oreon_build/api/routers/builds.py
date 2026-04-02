"""Builds - list/view public; trigger/cancel require maintainer or admin."""
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse

from oreon_build.utils.spec import name_from_srpm_filename, parse_spec_content
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import selectinload

from oreon_build.api.deps import CurrentUser, DbSession
from oreon_build.api.schemas import (
    ArtifactResponse,
    BuildAttemptResponse,
    BuildJobResponse,
    BuildTrigger,
    PaginatedResponse,
)
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import BuildAttempt, BuildJob, BuildStatus, BuildTarget, Package, Release, Worker
from oreon_build.services.r2 import get_r2_client, upload_r2_key

router = APIRouter(prefix="/builds", tags=["builds"])

# Default architectures for multi-arch builds
DEFAULT_ARCHITECTURES = ["x86_64", "aarch64"]


async def _get_or_create_build_target(db: DbSession, release_id: int, architecture: str, mock_environment_id: int | None) -> BuildTarget:
    """Get or create a BuildTarget for (release_id, architecture, mock_environment_id)."""
    from oreon_build.models import MockEnvironment
    q = (
        select(BuildTarget)
        .where(
            BuildTarget.release_id == release_id,
            BuildTarget.architecture == architecture,
            BuildTarget.mock_environment_id == mock_environment_id,
        )
        .order_by(BuildTarget.id.asc())
        .limit(1)
    )
    result = await db.execute(q)
    target = result.scalars().first()
    if target:
        return target
    # Use first mock env for release if none specified
    mock_env_id = mock_environment_id
    if mock_env_id is None:
        fallback = await db.execute(
            select(MockEnvironment).where(MockEnvironment.release_id == release_id).limit(1)
        )
        me = fallback.scalar_one_or_none()
        if me:
            mock_env_id = me.id
    target = BuildTarget(
        release_id=release_id,
        architecture=architecture,
        mock_environment_id=mock_env_id,
    )
    db.add(target)
    await db.flush()
    return target


def _build_job_search_clause(search_trim: str):
    term = search_trim.strip()
    if not term:
        return None
    esc = term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    pat = f"%{esc}%"
    parts = [
        Package.name.ilike(pat, escape="\\"),
        Release.releasename.ilike(pat, escape="\\"),
        cast(BuildJob.status, String).ilike(pat, escape="\\"),
        BuildTarget.architecture.ilike(pat, escape="\\"),
    ]
    if term.isdigit():
        n = int(term)
        parts.extend(
            [
                BuildJob.id == n,
                BuildJob.package_id == n,
                BuildJob.release_id == n,
            ]
        )
    return or_(*parts)


@router.get("", response_model=PaginatedResponse[BuildJobResponse])
async def list_builds(
    db: DbSession,
    release_id: int | None = None,
    package_id: int | None = None,
    status: str | None = None,
    search: str | None = Query(None, max_length=200, description="Filter by job id, package name, release name, arch, status"),
    limit: int = Query(25, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """List build jobs. Query params: release_id, package_id, status, search, limit, offset."""
    q = select(BuildJob).options(selectinload(BuildJob.build_target)).order_by(BuildJob.created_at.desc())
    count_q = select(func.count()).select_from(BuildJob)
    search_trim = (search or "").strip()
    if search_trim:
        q = (
            q.join(Package, BuildJob.package_id == Package.id)
            .join(Release, BuildJob.release_id == Release.id)
            .outerjoin(BuildTarget, BuildJob.target_id == BuildTarget.id)
        )
        count_q = (
            select(func.count())
            .select_from(BuildJob)
            .join(Package, BuildJob.package_id == Package.id)
            .join(Release, BuildJob.release_id == Release.id)
            .outerjoin(BuildTarget, BuildJob.target_id == BuildTarget.id)
        )
        sc = _build_job_search_clause(search_trim)
        if sc is not None:
            q = q.where(sc)
            count_q = count_q.where(sc)
    if release_id is not None:
        q = q.where(BuildJob.release_id == release_id)
        count_q = count_q.where(BuildJob.release_id == release_id)
    if package_id is not None:
        q = q.where(BuildJob.package_id == package_id)
        count_q = count_q.where(BuildJob.package_id == package_id)
    if status is not None:
        q = q.where(BuildJob.status == status)
        count_q = count_q.where(BuildJob.status == status)
    total = (await db.execute(count_q)).scalar() or 0
    q = q.limit(limit).offset(offset)
    result = await db.execute(q)
    jobs = list(result.scalars().all())
    return PaginatedResponse(items=jobs, total=total)


@router.get("/jobs/{job_id}", response_model=BuildJobResponse)
async def get_build_job(job_id: int, db: DbSession):
    result = await db.execute(select(BuildJob).where(BuildJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build job not found")
    return await _job_to_response(db, job)


@router.get("/jobs/{job_id}/attempts", response_model=list[BuildAttemptResponse])
async def list_attempts(job_id: int, db: DbSession):
    result = await db.execute(
        select(BuildAttempt).where(BuildAttempt.build_job_id == job_id).order_by(BuildAttempt.started_at.desc())
    )
    return list(result.scalars().all())


@router.get("/attempts/{attempt_id}/artifacts", response_model=list[ArtifactResponse])
async def list_artifacts(attempt_id: int, db: DbSession):
    from oreon_build.models import Artifact
    result = await db.execute(select(Artifact).where(Artifact.build_attempt_id == attempt_id))
    return list(result.scalars().all())


def _source_url_from_new_package(np) -> str | None:
    """Primary URL for Source record from NewPackageForBuild."""
    if np.source_url:
        return np.source_url
    if np.source_urls:
        return np.source_urls[0]
    return None


def _infer_github_html_url_from_new_package(np) -> str | None:
    """Best-effort https://github.com/org/repo from distgit or scm URL fragment."""
    u = _source_url_from_new_package(np)
    if not u or "github.com" not in u.lower():
        return None
    base = u.split("#", 1)[0].strip().rstrip("/")
    if base.lower().endswith(".git"):
        base = base[:-4]
    if not base.lower().startswith("http"):
        return None
    return base


@router.post("/trigger", response_model=list[BuildJobResponse], status_code=status.HTTP_201_CREATED)
async def trigger_build(
    data: BuildTrigger,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from datetime import datetime, timezone
    from oreon_build.models import Package, PackageVersion, Release, Source

    package_id: int
    package_version_id: int | None = data.package_version_id
    preferred_mock_environment_id: int | None = None

    if data.new_package:
        np = data.new_package
        from oreon_build.models import MockEnvironment as MockEnv
        if np.preferred_mock_environment_id:
            me_r = await db.execute(select(MockEnv).where(MockEnv.id == np.preferred_mock_environment_id))
            me = me_r.scalar_one_or_none()
            if not me or me.release_id != data.release_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Mock environment not found or does not belong to the selected release.",
                )
        existing = await db.execute(select(Package).where(Package.name == np.name))
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Package name already exists: {np.name}",
            )
        gh_url = np.github_html_url or _infer_github_html_url_from_new_package(np)
        pkg = Package(
            name=np.name,
            description=np.description,
            preferred_mock_environment_id=np.preferred_mock_environment_id,
            github_repository_id=np.github_repository_id,
            github_html_url=gh_url,
        )
        db.add(pkg)
        await db.flush()
        package_id = pkg.id

        pv = PackageVersion(
            package_id=pkg.id,
            version=np.version or "0.0.1",
        )
        db.add(pv)
        await db.flush()
        package_version_id = pv.id

        primary_url = _source_url_from_new_package(np)
        if primary_url:
            db.add(Source(package_version_id=pv.id, kind=np.source_type.value, url=primary_url))
        for u in (np.source_urls or [])[1:]:
            db.add(Source(package_version_id=pv.id, kind="url", url=u))
        await db.flush()

        await log_audit(
            db,
            "package.create",
            account_id=user.id,
            resource_type="package",
            resource_id=str(pkg.id),
            details=np.name,
            ip_address=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
        preferred_mock_environment_id = np.preferred_mock_environment_id
    else:
        package_id = data.package_id
        rp = await db.execute(
            select(Package).where(Package.id == package_id).options(selectinload(Package.preferred_mock_environment))
        )
        pkg = rp.scalar_one_or_none()
        if not pkg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
        preferred_mock_environment_id = pkg.preferred_mock_environment_id if pkg.preferred_mock_environment_id else None

    rr = await db.execute(select(Release).where(Release.id == data.release_id))
    release_for_job = rr.scalar_one_or_none()
    if not release_for_job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    publish_branch = r2_publish_branch(release_for_job)

    architectures = data.architectures or DEFAULT_ARCHITECTURES
    if not architectures:
        architectures = DEFAULT_ARCHITECTURES

    workers_result = await db.execute(select(Worker))
    workers = list(workers_result.scalars().all())
    workers_by_arch: dict[str, bool] = {}
    for w in workers:
        arch = (w.architecture or "").strip() or "x86_64"
        workers_by_arch[arch] = True

    jobs_created: list[BuildJob] = []
    for arch in architectures:
        arch = (arch or "").strip()
        if not arch:
            continue
        has_worker = workers_by_arch.get(arch, False)
        if not has_worker:
            continue  # Skip arch with no worker; only create jobs that can run
        target = await _get_or_create_build_target(db, data.release_id, arch, preferred_mock_environment_id)
        job = BuildJob(
            package_id=package_id,
            release_id=data.release_id,
            target_id=target.id,
            package_version_id=package_version_id,
            status=BuildStatus.PENDING,
            priority=data.priority,
            triggered_by_id=user.id,
            branch=publish_branch,
        )
        db.add(job)
        await db.flush()
        jobs_created.append(job)

    if not jobs_created:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No worker available for any selected architecture. Enroll a worker for at least one selected arch.",
        )
    for job in jobs_created:
        await log_audit(
            db,
            "build.trigger",
            account_id=user.id,
            resource_type="build_job",
            resource_id=str(job.id),
            details=f"package_id={package_id} release_id={data.release_id}",
            ip_address=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
    return jobs_created


ALLOWED_UPLOAD_EXTENSIONS = (".src.rpm", ".spec")
MAX_UPLOAD_MB = 100


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_and_trigger(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    name: str | None = Form(None, max_length=256),
    description: str | None = Form(None),
    release_id: int = Form(...),
    preferred_mock_environment_id: int | None = Form(None),
    architectures: str = Form("x86_64,aarch64", description="Comma-separated, e.g. x86_64,aarch64"),
    files: list[UploadFile] = File(...),
):
    """Upload SRPM and/or spec files, create package and version, trigger build(s) per architecture."""
    from datetime import datetime, timezone
    from oreon_build.models import BuildAttempt, MockEnvironment, Package, PackageVersion, Release, Source

    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one file (SRPM or .spec) required")
    for f in files:
        fn = (f.filename or "").lower()
        if not any(fn.endswith(ext) for ext in ALLOWED_UPLOAD_EXTENSIONS):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File must be .src.rpm or .spec; got {f.filename}",
            )

    # Read all files and derive name/description from spec when not provided
    parsed_name: str | None = None
    parsed_summary: str | None = None
    file_contents: list[tuple[str, bytes]] = []
    for up in files:
        fn = up.filename or "file"
        content = await up.read()
        if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"File {fn} exceeds {MAX_UPLOAD_MB}MB")
        file_contents.append((fn, content))
        if fn.lower().endswith(".spec") and (parsed_name is None or parsed_summary is None):
            info = parse_spec_content(content)
            if info.name:
                parsed_name = info.name
            if info.summary:
                parsed_summary = info.summary
    # Resolve package name: form > parsed from spec > from first SRPM filename
    pkg_name = (name or "").strip() or parsed_name
    if not pkg_name:
        for fn, _ in file_contents:
            pkg_name = name_from_srpm_filename(fn)
            if pkg_name:
                break
    if not pkg_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Package name could not be determined. Provide a .spec file (Name: is parsed) or specify name.",
        )
    pkg_desc = (description or "").strip() or parsed_summary or None

    rr = await db.execute(select(Release).where(Release.id == release_id))
    release = rr.scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    releasename = release.releasename

    mock_env_id: int | None = preferred_mock_environment_id
    if mock_env_id:
        me_result = await db.execute(select(MockEnvironment).where(MockEnvironment.id == mock_env_id))
        me = me_result.scalar_one_or_none()
        if not me or me.release_id != release_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Mock environment not found or does not belong to the selected release.",
            )

    existing = await db.execute(select(Package).where(Package.name == pkg_name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Package name already exists: {pkg_name}")

    pkg = Package(name=pkg_name, description=pkg_desc, preferred_mock_environment_id=mock_env_id)
    db.add(pkg)
    await db.flush()

    pv = PackageVersion(package_id=pkg.id, version="0.0.1")
    db.add(pv)
    await db.flush()

    r2 = get_r2_client()
    upload_id = uuid.uuid4().hex
    for fn, content in file_contents:
        key = upload_r2_key(releasename, pkg_name, upload_id, fn)
        r2.put_object(key, content)
        kind = "upload_srpm" if fn.lower().endswith(".src.rpm") else "upload_spec"
        db.add(Source(package_version_id=pv.id, kind=kind, r2_key=key))
    await db.flush()

    await log_audit(
        db,
        "package.create",
        account_id=user.id,
        resource_type="package",
        resource_id=str(pkg.id),
        details=pkg_name,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )

    arch_list = [a.strip() for a in architectures.split(",") if a.strip()] or DEFAULT_ARCHITECTURES
    if not arch_list:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one architecture must be selected.",
        )
    workers_result = await db.execute(select(Worker))
    workers = list(workers_result.scalars().all())
    workers_by_arch = {}
    for w in workers:
        arch = (w.architecture or "").strip() or "x86_64"
        workers_by_arch[arch] = True

    jobs_created = []
    for arch in arch_list:
        has_worker = workers_by_arch.get(arch, False)
        if not has_worker:
            continue  # Skip arch with no worker; only create jobs that can run
        target = await _get_or_create_build_target(db, release_id, arch, mock_env_id)
        job = BuildJob(
            package_id=pkg.id,
            release_id=release_id,
            target_id=target.id,
            package_version_id=pv.id,
            status=BuildStatus.PENDING,
            priority=0,
            triggered_by_id=user.id,
            branch=r2_publish_branch(release),
        )
        db.add(job)
        await db.flush()
        jobs_created.append(job)
        await log_audit(
            db,
            "build.trigger",
            account_id=user.id,
            resource_type="build_job",
            resource_id=str(job.id),
            details=f"package_id={pkg.id} release_id={release_id} upload",
            ip_address=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
    if not jobs_created:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No worker available for any selected architecture. Enroll a worker for at least one selected arch.",
        )
    first = jobs_created[0]
    # Get architecture without touching job.build_target (avoids async lazy-load)
    arch = None
    if first.target_id:
        arch_row = await db.execute(
            select(BuildTarget.architecture).where(BuildTarget.id == first.target_id)
        )
        arch = arch_row.scalars().one_or_none()
    payload = BuildJobResponse(
        id=first.id,
        package_id=first.package_id,
        release_id=first.release_id,
        target_id=first.target_id,
        architecture=arch,
        package_version_id=first.package_version_id,
        status=first.status.value,
        priority=first.priority,
        created_at=first.created_at,
        completed_at=first.completed_at,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=payload.model_dump(mode="json"),
    )


async def _job_to_response(db: DbSession, job: BuildJob) -> BuildJobResponse:
    """Build BuildJobResponse without touching job.build_target (avoids async lazy-load)."""
    arch = None
    if job.target_id:
        r = await db.execute(select(BuildTarget.architecture).where(BuildTarget.id == job.target_id))
        arch = r.scalars().one_or_none()
    return BuildJobResponse(
        id=job.id,
        package_id=job.package_id,
        release_id=job.release_id,
        target_id=job.target_id,
        architecture=arch,
        package_version_id=job.package_version_id,
        status=job.status.value,
        priority=job.priority,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )


@router.post("/jobs/{job_id}/retry")
async def retry_build(
    job_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    result = await db.execute(select(BuildJob).where(BuildJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build job not found")
    job.status = BuildStatus.PENDING
    job.completed_at = None
    await log_audit(
        db,
        "build.retry",
        account_id=user.id,
        resource_type="build_job",
        resource_id=str(job_id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    payload = await _job_to_response(db, job)
    return JSONResponse(status_code=200, content=payload.model_dump(mode="json"))


@router.post("/jobs/{job_id}/cancel")
async def cancel_build(
    job_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from datetime import datetime, timezone

    result = await db.execute(select(BuildJob).where(BuildJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build job not found")
    if job.status not in (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Job not in cancellable state")
    job.status = BuildStatus.CANCELLED
    job.completed_at = datetime.now(timezone.utc)
    # Mark in-flight attempts cancelled so UI does not show "running" forever; worker will stop mock when it polls cancel-check.
    att_result = await db.execute(
        select(BuildAttempt).where(
            BuildAttempt.build_job_id == job_id,
            BuildAttempt.status == BuildStatus.RUNNING,
        )
    )
    now = datetime.now(timezone.utc)
    for att in att_result.scalars().all():
        att.status = BuildStatus.CANCELLED
        att.finished_at = now
        att.error_message = "Cancelled by user"
    await log_audit(
        db,
        "build.cancel",
        account_id=user.id,
        resource_type="build_job",
        resource_id=str(job_id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    payload = await _job_to_response(db, job)
    return JSONResponse(status_code=200, content=payload.model_dump(mode="json"))
