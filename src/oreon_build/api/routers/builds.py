"""Builds - list/view public; trigger/cancel require maintainer or admin."""
import logging
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse

from oreon_build.utils.spec import name_from_srpm_filename, parse_spec_content
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import selectinload

from oreon_build.api.deps import CurrentAdmin, CurrentUser, DbSession
from oreon_build.api.schemas import (
    ArtifactResponse,
    BuildAttemptResponse,
    BuildJobResponse,
    BuildTrigger,
    ChainBuildTrigger,
    ChainBuildTriggerResponse,
    ChainResumeRequest,
    ChainStatusResponse,
    CurrentFailureItem,
    PaginatedResponse,
    RpmbuildPlanCreate,
    RpmbuildPlanResponse,
)
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import BuildAttempt, BuildChain, BuildJob, BuildStatus, BuildTarget, Package, Release, RpmbuildPlan, Worker
from oreon_build.services.build_target_util import (
    get_or_create_build_target,
    has_successful_build_for_target,
    latest_package_version_id,
)
from oreon_build.services.r2 import get_r2_client, upload_r2_key

router = APIRouter(prefix="/builds", tags=["builds"])

logger = logging.getLogger(__name__)

# Default architectures for multi-arch builds
DEFAULT_ARCHITECTURES = ["x86_64", "aarch64"]


def _attempt_display_status(a: BuildAttempt) -> str:
    if a.status == BuildStatus.SUCCESS:
        return "success"
    if a.mock_succeeded_at is not None and a.status == BuildStatus.RUNNING:
        return "success"
    return a.status.value


def _job_display_status(job: BuildJob, latest: BuildAttempt | None) -> str:
    if job.status == BuildStatus.SUCCESS:
        return "success"
    if latest is not None and latest.mock_succeeded_at is not None and job.status == BuildStatus.RUNNING:
        return "success"
    return job.status.value


async def _latest_attempt_by_job_ids(db: DbSession, job_ids: list[int]) -> dict[int, BuildAttempt]:
    if not job_ids:
        return {}
    r = await db.execute(
        select(BuildAttempt)
        .where(BuildAttempt.build_job_id.in_(job_ids))
        .order_by(BuildAttempt.build_job_id.asc(), BuildAttempt.id.desc())
    )
    out: dict[int, BuildAttempt] = {}
    for att in r.scalars().all():
        if att.build_job_id not in out:
            out[att.build_job_id] = att
    return out


def _normalize_signing_status(raw: str) -> str:
    if raw == "uploading":
        return "running"
    return raw


def _effective_signing_status_attempt(a: BuildAttempt) -> str:
    """Infer controller pipeline phase when DB still has default not_started (older rows or no mock-complete)."""
    raw = _normalize_signing_status((a.signing_status or "").strip() or "not_started")
    if raw != "not_started":
        return raw
    if a.status == BuildStatus.SUCCESS:
        return "signed"
    if a.status in (BuildStatus.FAILED, BuildStatus.CANCELLED, BuildStatus.SKIPPED):
        return "failed"
    return "not_started"


def _effective_signing_status_job(job: BuildJob, latest: BuildAttempt | None) -> str | None:
    """Prefer stored status; for finished jobs with default not_started, infer from job + attempt."""
    if latest is None:
        return None
    raw = _normalize_signing_status((latest.signing_status or "").strip() or "not_started")
    if raw != "not_started":
        return raw
    if job.status == BuildStatus.SUCCESS:
        return "signed"
    if job.status in (BuildStatus.FAILED, BuildStatus.CANCELLED, BuildStatus.SKIPPED):
        return "failed"
    if job.status == BuildStatus.RUNNING and latest.mock_succeeded_at is not None:
        return "running"
    return "not_started"


def _attempt_to_response(a: BuildAttempt) -> BuildAttemptResponse:
    w = a.worker
    return BuildAttemptResponse(
        id=a.id,
        build_job_id=a.build_job_id,
        worker_id=a.worker_id,
        attempt_number=a.attempt_number,
        status=a.status.value,
        display_status=_attempt_display_status(a),
        mock_succeeded_at=a.mock_succeeded_at,
        signing_status=_effective_signing_status_attempt(a),
        started_at=a.started_at,
        finished_at=a.finished_at,
        log_r2_key=a.log_r2_key,
        error_message=a.error_message,
        created_at=a.created_at,
        worker_name=w.name if w else None,
        worker_last_heartbeat=w.last_heartbeat if w else None,
    )


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


@router.get("/current-failures", response_model=list[CurrentFailureItem])
async def list_current_failures(db: DbSession):
    """
    Jobs whose **latest** row per (package, release, target) is ``failed``.

    Older successful or failed jobs for the same slot are ignored so this reflects only what
    still needs attention today.
    """
    ranked = (
        select(
            BuildJob.id.label("job_id"),
            func.row_number()
            .over(
                partition_by=(
                    BuildJob.package_id,
                    BuildJob.release_id,
                    func.coalesce(BuildJob.target_id, -1),
                ),
                order_by=BuildJob.id.desc(),
            )
            .label("rn"),
        )
        .subquery()
    )
    stmt = (
        select(BuildJob, Package.name, Release.releasename)
        .join(ranked, BuildJob.id == ranked.c.job_id)
        .join(Package, Package.id == BuildJob.package_id)
        .join(Release, Release.id == BuildJob.release_id)
        .where(ranked.c.rn == 1)
        .where(BuildJob.status == BuildStatus.FAILED)
        .order_by(BuildJob.completed_at.desc().nullslast(), BuildJob.id.desc())
    )
    result = await db.execute(stmt)
    rows = result.all()
    if not rows:
        return []

    jobs: list[BuildJob] = [r[0] for r in rows]
    job_ids = [j.id for j in jobs]
    att_rows = await db.execute(
        select(BuildAttempt)
        .where(BuildAttempt.build_job_id.in_(job_ids))
        .order_by(BuildAttempt.build_job_id.asc(), BuildAttempt.id.desc())
    )
    latest_by_job: dict[int, BuildAttempt] = {}
    for att in att_rows.scalars().all():
        if att.build_job_id not in latest_by_job:
            latest_by_job[att.build_job_id] = att

    tid_set = {j.target_id for j in jobs if j.target_id}
    arch_map: dict[int, str] = {}
    if tid_set:
        ar = await db.execute(
            select(BuildTarget.id, BuildTarget.architecture).where(BuildTarget.id.in_(tid_set))
        )
        for tid, architecture in ar.all():
            arch_map[int(tid)] = architecture

    out: list[CurrentFailureItem] = []
    for job, pkg_name, rel_name in rows:
        arch = arch_map.get(job.target_id) if job.target_id else None
        att = latest_by_job.get(job.id)
        out.append(
            CurrentFailureItem(
                id=job.id,
                package_id=job.package_id,
                package_name=pkg_name or "",
                release_id=job.release_id,
                release_name=rel_name or "",
                target_id=job.target_id,
                architecture=arch,
                package_version_id=job.package_version_id,
                status=job.status.value,
                priority=job.priority,
                created_at=job.created_at,
                completed_at=job.completed_at,
                last_attempt_id=att.id if att else None,
                last_error_message=(att.error_message[:2000] if att and att.error_message else None),
            )
        )
    return out


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
    att_map = await _latest_attempt_by_job_ids(db, [j.id for j in jobs])
    items = [await _job_to_response(db, j, att_map.get(j.id)) for j in jobs]
    return PaginatedResponse(items=items, total=total)


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
        select(BuildAttempt)
        .where(BuildAttempt.build_job_id == job_id)
        .options(selectinload(BuildAttempt.worker))
        .order_by(BuildAttempt.started_at.desc())
    )
    rows = list(result.scalars().all())
    return [_attempt_to_response(a) for a in rows]


@router.get("/attempts/{attempt_id}/artifacts", response_model=list[ArtifactResponse])
async def list_artifacts(attempt_id: int, db: DbSession):
    from oreon_build.models import Artifact
    result = await db.execute(select(Artifact).where(Artifact.build_attempt_id == attempt_id))
    return list(result.scalars().all())


@router.get("/distgit-checklist")
async def distgit_checklist(
    release_id: int,
    db: DbSession,
    user: CurrentUser,
):
    """Return packages in the distgit repo (oreonhq/rpm-specfiles) that:
    - have a spec file at <releasename>/<pkg>/<pkg>.spec
    - do NOT have a dead.package marker
    - do NOT yet have a successful build for this release

    Uses the GitHub git-tree API (one call) to enumerate the full repo tree.
    Returns {packages: [{name, spec_path}], truncated: bool}.
    """
    from oreon_build.services.github import get_github_client

    rel_res = await db.execute(select(Release).where(Release.id == release_id))
    rel = rel_res.scalar_one_or_none()
    if not rel:
        raise HTTPException(status_code=404, detail="Release not found")

    releasename = (rel.releasename or "").strip()
    if not releasename:
        raise HTTPException(status_code=400, detail="Release has no releasename configured")

    owner = "oreonhq"
    repo = "rpm-specfiles"
    branch = "main"
    client = get_github_client()
    try:
        tree_data = await client.list_git_tree_recursive(owner, repo, branch)
    except Exception as exc:
        logger.warning("distgit-checklist: GitHub tree fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"GitHub API error: {exc}") from exc
    finally:
        await client.close()

    tree: list[dict] = tree_data.get("tree") or []
    truncated: bool = bool(tree_data.get("truncated"))

    prefix = releasename + "/"
    has_spec: set[str] = set()
    is_dead: set[str] = set()

    for entry in tree:
        path: str = entry.get("path") or ""
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        parts = rest.split("/", 1)
        if len(parts) != 2:
            continue
        pkg_dir, filename = parts
        if not pkg_dir or not filename:
            continue
        if filename == f"{pkg_dir}.spec":
            has_spec.add(pkg_dir)
        elif filename == "dead.package":
            is_dead.add(pkg_dir)

    valid_names = sorted(has_spec - is_dead)

    # "Already built" means has SUCCESS and no FAILED/CANCELLED jobs for this release.
    # Failed or cancelled packages stay visible in the checklist for retry.
    if valid_names:
        success_q = (
            select(Package.name)
            .join(BuildJob, BuildJob.package_id == Package.id)
            .where(
                BuildJob.release_id == release_id,
                BuildJob.status == BuildStatus.SUCCESS,
                Package.name.in_(valid_names),
            )
            .distinct()
        )
        already_res = await db.execute(success_q)
        success_names: set[str] = {str(row[0]) for row in already_res.all() if row[0]}
        failed_or_cancelled_q = (
            select(Package.name)
            .join(BuildJob, BuildJob.package_id == Package.id)
            .where(
                BuildJob.release_id == release_id,
                BuildJob.status.in_((BuildStatus.FAILED, BuildStatus.CANCELLED)),
                Package.name.in_(valid_names),
            )
            .distinct()
        )
        failed_or_cancelled_res = await db.execute(failed_or_cancelled_q)
        failed_or_cancelled_names: set[str] = {str(row[0]) for row in failed_or_cancelled_res.all() if row[0]}
        already_built: set[str] = success_names - failed_or_cancelled_names
    else:
        already_built = set()

    unbuilt = [
        {"name": name, "spec_path": f"{releasename}/{name}/{name}.spec"}
        for name in valid_names
        if name not in already_built
    ]

    return {
        "packages": unbuilt,
        "total": len(unbuilt),
        "already_built_count": len(already_built),
        "truncated": truncated,
        "releasename": releasename,
    }


@router.get("/layers-checklist")
async def layers_checklist(
    release_id: int,
    db: DbSession,
    user: CurrentUser,
    distgit_url: str | None = Query(None, max_length=1024),
    distgit_branch: str | None = Query(None, max_length=128),
) -> dict:
    """Return rpmbuild-order layers from distgit specs with per-package ``finished``.

    Used by the Builds page Layers tab. A package is finished only when its latest attempt
    status for this release is SUCCESS. If latest attempt is FAILED/CANCELLED (or not success),
    it stays actionable so hide-finished remains accurate after retries.
    """
    from oreon_build.config import get_settings
    from oreon_build.services.distgit_order import compute_distgit_build_order

    rel_res = await db.execute(select(Release).where(Release.id == release_id))
    rel = rel_res.scalar_one_or_none()
    if not rel:
        raise HTTPException(status_code=404, detail="Release not found")
    releasename = (rel.releasename or "").strip()
    if not releasename:
        raise HTTPException(status_code=400, detail="Release has no releasename configured")

    s = get_settings()
    dg = (distgit_url or getattr(s, "mass_rebuild_distgit_url", None) or "https://github.com/oreonhq/rpm-specfiles.git").strip()
    br = (distgit_branch or getattr(s, "mass_rebuild_distgit_branch", None) or "main").strip()

    order = await compute_distgit_build_order(releasename=releasename, distgit_url=dg, distgit_branch=br)
    flat_names: list[str] = []
    for wave in order.layers:
        for n in wave:
            n = str(n).strip()
            if n and n not in flat_names:
                flat_names.append(n)

    already_built: set[str] = set()
    if flat_names:
        latest_attempt_rows = await db.execute(
            select(Package.name, BuildAttempt.status, BuildAttempt.id)
            .join(BuildJob, BuildAttempt.build_job_id == BuildJob.id)
            .join(Package, BuildJob.package_id == Package.id)
            .where(
                BuildJob.release_id == release_id,
                Package.name.in_(flat_names),
            )
            .order_by(Package.name.asc(), BuildAttempt.id.desc())
        )
        latest_attempt_by_pkg: dict[str, BuildStatus] = {}
        for pkg_name, attempt_status, _attempt_id in latest_attempt_rows.all():
            n = str(pkg_name or "").strip()
            if not n or n in latest_attempt_by_pkg:
                continue
            latest_attempt_by_pkg[n] = attempt_status

        latest_job_rows = await db.execute(
            select(Package.name, BuildJob.status, BuildJob.id)
            .join(BuildJob, BuildJob.package_id == Package.id)
            .where(
                BuildJob.release_id == release_id,
                Package.name.in_(flat_names),
            )
            .order_by(Package.name.asc(), BuildJob.id.desc())
        )
        latest_job_by_pkg: dict[str, BuildStatus] = {}
        for pkg_name, job_status, _job_id in latest_job_rows.all():
            n = str(pkg_name or "").strip()
            if not n or n in latest_job_by_pkg:
                continue
            latest_job_by_pkg[n] = job_status

        for n in flat_names:
            st = latest_attempt_by_pkg.get(n)
            if st is None:
                st = latest_job_by_pkg.get(n)
            if st == BuildStatus.SUCCESS:
                already_built.add(n)

    layers_out: list[list[dict[str, object]]] = []
    for wave in order.layers:
        row: list[dict[str, object]] = []
        for name in wave:
            name = str(name).strip()
            if not name:
                continue
            row.append(
                {
                    "name": name,
                    "finished": name in already_built,
                    "spec_path": f"{releasename}/{name}/{name}.spec",
                }
            )
        if row:
            layers_out.append(row)

    return {
        "releasename": releasename,
        "distgit_url": dg,
        "distgit_branch": br,
        "layers": layers_out,
        "errors": order.errors,
        "cycle_warning": order.cycle_warning,
    }


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
        existing_r = await db.execute(select(Package).where(Package.name == np.name))
        existing_pkg = existing_r.scalar_one_or_none()
        if existing_pkg:
            # DistGit/UI often sends new_package even when the package already exists (e.g. second arch).
            # Queue builds like a normal trigger instead of rejecting.
            package_id = existing_pkg.id
            preferred_mock_environment_id = (
                existing_pkg.preferred_mock_environment_id
                if existing_pkg.preferred_mock_environment_id
                else None
            )
        else:
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

    # Fall back to the release-level default mock env if neither the build
    # request nor the package carries a preference.
    if preferred_mock_environment_id is None and release_for_job.default_mock_environment_id:
        preferred_mock_environment_id = release_for_job.default_mock_environment_id

    if package_version_id is None:
        package_version_id = await latest_package_version_id(db, package_id)

    architectures = data.architectures or DEFAULT_ARCHITECTURES
    if not architectures:
        architectures = DEFAULT_ARCHITECTURES

    workers_result = await db.execute(select(Worker))
    workers = list(workers_result.scalars().all())
    workers_by_arch: dict[str, bool] = {}
    for w in workers:
        workers_by_arch[normalize_machine_arch(w.architecture)] = True

    jobs_created: list[BuildJob] = []
    saw_worker_for_requested = False
    for arch in architectures:
        arch = (arch or "").strip()
        if not arch:
            continue
        canon = normalize_machine_arch(arch)
        has_worker = workers_by_arch.get(canon, False)
        if not has_worker:
            continue  # Skip arch with no worker; only create jobs that can run
        saw_worker_for_requested = True
        target = await get_or_create_build_target(db, data.release_id, canon, preferred_mock_environment_id)
        if data.skip_complete_arches and await has_successful_build_for_target(
            db, package_id, data.release_id, target.id, package_version_id
        ):
            continue
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
        req_norm = sorted(
            {normalize_machine_arch(a) for a in architectures if (a or "").strip()}
        )
        avail_norm = sorted(workers_by_arch.keys())
        if not saw_worker_for_requested:
            detail = (
                "No worker enrolled for any selected architecture. "
                f"Requested: {', '.join(req_norm) if req_norm else '(none)'}. "
                f"Workers cover: {', '.join(avail_norm) if avail_norm else '(no workers)'}. "
                "Enroll a worker for at least one selected arch or fix its architecture field."
            )
            logger.warning("build.trigger rejected release_id=%s package_id=%s %s", data.release_id, package_id, detail)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
        logger.info(
            "build.trigger skip_all_already_ok release_id=%s package_id=%s requested=%s",
            data.release_id,
            package_id,
            req_norm,
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
    att_map = await _latest_attempt_by_job_ids(db, [j.id for j in jobs_created])
    return [await _job_to_response(db, j, att_map.get(j.id)) for j in jobs_created]


@router.post("/chain-trigger", response_model=ChainBuildTriggerResponse, status_code=status.HTTP_201_CREATED)
async def trigger_chain_build_endpoint(
    data: ChainBuildTrigger,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from oreon_build.services.build_chain import trigger_chain_build as run_chain_build

    chain, jobs, preparing = await run_chain_build(
        db,
        request,
        user,
        chain_spec=data.chain_spec,
        release_id=data.release_id,
        architectures=data.architectures,
        skip_complete_arches=data.skip_complete_arches,
        preferred_mock_environment_id=data.preferred_mock_environment_id,
        distgit_url=data.distgit_url,
        distgit_branch=data.distgit_branch,
        priority=data.priority,
    )
    att_map = await _latest_attempt_by_job_ids(db, [j.id for j in jobs])
    job_responses = [await _job_to_response(db, j, att_map.get(j.id)) for j in jobs]
    stages_raw = chain.stages_json if isinstance(chain.stages_json, list) else []
    stages: list[list[str]] = [[str(n) for n in grp] for grp in stages_raw if isinstance(grp, list)]
    return ChainBuildTriggerResponse(
        chain_id=chain.id,
        chain_status=chain.status.value,
        stages=stages,
        chain_spec=chain.chain_spec,
        jobs=job_responses,
        preparing=preparing,
    )


async def _chain_to_status_response(db: DbSession, chain: BuildChain) -> ChainStatusResponse:
    from oreon_build.services.build_chain import _chain_options_preparing, _options_dict

    # fail_chain/job cancels expire the row; refresh before reading scalar columns.
    await db.refresh(chain)

    stages_raw = chain.stages_json if isinstance(chain.stages_json, list) else []
    stages: list[list[str]] = [[str(n) for n in grp] for grp in stages_raw if isinstance(grp, list)]
    opt = _options_dict(chain)
    rel = await db.get(Release, chain.release_id)
    job_n = await db.scalar(select(func.count()).select_from(BuildJob).where(BuildJob.chain_id == chain.id))
    raw_unresolved = opt.get("unresolved_capabilities")
    unresolved: list[str] = []
    if isinstance(raw_unresolved, list):
        unresolved = [str(x) for x in raw_unresolved if x]
    stage0_raw = opt.get("prepare_stage0_jobs")
    prepare_stage0_jobs: int | None
    if stage0_raw is None or stage0_raw == "":
        prepare_stage0_jobs = None
    else:
        try:
            prepare_stage0_jobs = int(stage0_raw)
        except (TypeError, ValueError):
            prepare_stage0_jobs = None
    return ChainStatusResponse(
        chain_id=chain.id,
        status=chain.status.value,
        active_stage_index=chain.active_stage_index,
        stages=stages,
        chain_spec=chain.chain_spec,
        error_message=chain.error_message,
        preparing=_chain_options_preparing(opt, chain_status=chain.status),
        job_count=int(job_n or 0),
        releasename=(rel.releasename if rel else None),
        distgit_url=str(opt.get("distgit_url") or "") or None,
        distgit_branch=str(opt.get("distgit_branch") or "") or None,
        unresolved_capabilities=unresolved,
        prepare_started_at=str(opt.get("prepare_started_at") or "") or None,
        prepare_finished_at=str(opt.get("prepare_finished_at") or "") or None,
        prepare_stage0_jobs=prepare_stage0_jobs,
        created_at=chain.created_at,
        updated_at=chain.updated_at,
    )


@router.get("/chains", response_model=list[ChainStatusResponse])
async def list_chains(
    db: DbSession,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Recent distgit chain builds (newest first) with full prepare/error details."""
    result = await db.execute(
        select(BuildChain).order_by(BuildChain.id.desc()).limit(limit).offset(offset)
    )
    chains = list(result.scalars().all())
    return [await _chain_to_status_response(db, c) for c in chains]


@router.get("/chain/{chain_id}/status", response_model=ChainStatusResponse)
async def get_chain_status(chain_id: int, db: DbSession):
    result = await db.execute(select(BuildChain).where(BuildChain.id == chain_id))
    chain = result.scalar_one_or_none()
    if not chain:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chain not found")
    return await _chain_to_status_response(db, chain)


@router.post("/chain/{chain_id}/cancel", response_model=ChainStatusResponse)
async def cancel_chain_build_endpoint(
    chain_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from oreon_build.services.build_chain import cancel_chain_by_user

    chain = await cancel_chain_by_user(db, request, user, chain_id=chain_id)
    return await _chain_to_status_response(db, chain)


@router.post("/chain-resume", response_model=ChainBuildTriggerResponse, status_code=status.HTTP_201_CREATED)
async def resume_chain_build_endpoint(
    data: ChainResumeRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from oreon_build.services.build_chain import resume_chain_from_previous

    chain, jobs, preparing = await resume_chain_from_previous(
        db, request, user, from_chain_id=data.from_chain_id
    )
    att_map = await _latest_attempt_by_job_ids(db, [j.id for j in jobs])
    job_responses = [await _job_to_response(db, j, att_map.get(j.id)) for j in jobs]
    stages_raw = chain.stages_json if isinstance(chain.stages_json, list) else []
    stages: list[list[str]] = [[str(n) for n in grp] for grp in stages_raw if isinstance(grp, list)]
    return ChainBuildTriggerResponse(
        chain_id=chain.id,
        chain_status=chain.status.value,
        stages=stages,
        chain_spec=chain.chain_spec,
        jobs=job_responses,
        preparing=preparing,
    )


@router.post("/mass-rebuild/run", response_model=dict)
async def mass_rebuild_run_once(
    db: DbSession,
    admin: CurrentAdmin,
):
    from oreon_build.services.mass_rebuild import run_mass_rebuild_pass

    return await run_mass_rebuild_pass(db)


@router.post("/rpmbuild-plan", response_model=RpmbuildPlanResponse, status_code=status.HTTP_201_CREATED)
async def create_rpmbuild_plan_endpoint(
    data: RpmbuildPlanCreate,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from oreon_build.config import get_settings
    from oreon_build.services.rpmbuild_order import create_rpmbuild_plan

    s = get_settings()
    if not s.rpmbuild_layers_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="rpmbuild layers are disabled")
    out = await create_rpmbuild_plan(
        db,
        request,
        release_id=data.release_id,
        repo_branch=data.repo_branch,
        seed_packages=data.seed_packages,
        architectures=data.architectures,
        priority=data.priority,
        distgit_url=data.distgit_url,
        distgit_branch=data.distgit_branch,
        triggered_by_id=user.id,
    )
    qjobs = await db.scalar(select(func.count(BuildJob.id)).where(BuildJob.rpmbuild_plan_id == out.plan.id))
    return RpmbuildPlanResponse(
        id=out.plan.id,
        release_id=out.plan.release_id,
        releasename=out.plan.releasename,
        repo_branch=out.plan.repo_branch,
        distgit_url=out.plan.distgit_url,
        distgit_branch=out.plan.distgit_branch,
        seed_mode=out.plan.seed_mode,
        seed_packages=list(out.plan.seed_packages_json or []),
        status=out.plan.status.value if hasattr(out.plan.status, "value") else str(out.plan.status),
        current_open_layer=int(out.plan.current_open_layer or 0),
        total_layers=len(out.layers),
        queued_jobs=int(qjobs or 0),
        created_at=out.plan.created_at,
        updated_at=out.plan.updated_at,
    )


@router.get("/rpmbuild-plans", response_model=list[RpmbuildPlanResponse])
async def list_rpmbuild_plans(db: DbSession):
    rows = await db.execute(select(RpmbuildPlan).order_by(RpmbuildPlan.created_at.desc()).limit(200))
    plans = list(rows.scalars().all())
    out: list[RpmbuildPlanResponse] = []
    for p in plans:
        qjobs = await db.scalar(select(func.count(BuildJob.id)).where(BuildJob.rpmbuild_plan_id == p.id))
        total_layers = len(p.layers_json or []) if isinstance(p.layers_json, list) else 0
        out.append(
            RpmbuildPlanResponse(
                id=p.id,
                release_id=p.release_id,
                releasename=p.releasename,
                repo_branch=p.repo_branch,
                distgit_url=p.distgit_url,
                distgit_branch=p.distgit_branch,
                seed_mode=p.seed_mode,
                seed_packages=list(p.seed_packages_json or []),
                status=p.status.value if hasattr(p.status, "value") else str(p.status),
                current_open_layer=int(p.current_open_layer or 0),
                total_layers=total_layers,
                queued_jobs=int(qjobs or 0),
                created_at=p.created_at,
                updated_at=p.updated_at,
            )
        )
    return out


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
        workers_by_arch[normalize_machine_arch(w.architecture)] = True

    jobs_created = []
    for arch in arch_list:
        canon = normalize_machine_arch(arch)
        has_worker = workers_by_arch.get(canon, False)
        if not has_worker:
            continue  # Skip arch with no worker; only create jobs that can run
        target = await get_or_create_build_target(db, release_id, canon, mock_env_id)
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
        req_norm = sorted({normalize_machine_arch(a) for a in arch_list if (a or "").strip()})
        avail_norm = sorted(workers_by_arch.keys())
        detail = (
            "No worker enrolled for any selected architecture. "
            f"Requested: {', '.join(req_norm) if req_norm else '(none)'}. "
            f"Workers cover: {', '.join(avail_norm) if avail_norm else '(no workers)'}. "
            "Enroll a worker for at least one selected arch or fix its architecture field."
        )
        logger.warning("build.upload trigger rejected release_id=%s package_id=%s %s", release_id, pkg.id, detail)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    first = jobs_created[0]
    payload = await _job_to_response(db, first)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=payload.model_dump(mode="json"),
    )


async def _job_to_response(
    db: DbSession, job: BuildJob, latest: BuildAttempt | None = None
) -> BuildJobResponse:
    """Build BuildJobResponse without touching job.build_target (avoids async lazy-load)."""
    if latest is None:
        r = await db.execute(
            select(BuildAttempt)
            .where(BuildAttempt.build_job_id == job.id)
            .order_by(BuildAttempt.id.desc())
            .limit(1)
        )
        latest = r.scalar_one_or_none()
    arch = None
    if job.target_id:
        r = await db.execute(select(BuildTarget.architecture).where(BuildTarget.id == job.target_id))
        arch = r.scalars().one_or_none()
    package_name = await db.scalar(select(Package.name).where(Package.id == job.package_id))
    release_name = await db.scalar(select(Release.releasename).where(Release.id == job.release_id))
    return BuildJobResponse(
        id=job.id,
        package_id=job.package_id,
        package_name=package_name,
        release_id=job.release_id,
        release_name=release_name,
        target_id=job.target_id,
        architecture=arch,
        package_version_id=job.package_version_id,
        status=job.status.value,
        display_status=_job_display_status(job, latest),
        signing_status=_effective_signing_status_job(job, latest),
        priority=job.priority,
        created_at=job.created_at,
        completed_at=job.completed_at,
        chain_id=getattr(job, "chain_id", None),
        chain_stage_index=getattr(job, "chain_stage_index", None),
        rpmbuild_plan_id=getattr(job, "rpmbuild_plan_id", None),
        rpmbuild_layer_index=getattr(job, "rpmbuild_layer_index", None),
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

    # Re-resolve target on retry so package mock-env changes are respected.
    pkg_result = await db.execute(select(Package).where(Package.id == job.package_id))
    pkg = pkg_result.scalar_one_or_none()
    if pkg is not None:
        target_arch: str | None = None
        if job.target_id:
            t_result = await db.execute(
                select(BuildTarget.architecture).where(BuildTarget.id == job.target_id)
            )
            target_arch = t_result.scalars().one_or_none()
        if target_arch:
            preferred_env_id = pkg.preferred_mock_environment_id
            if preferred_env_id is not None:
                from oreon_build.models import MockEnvironment

                me_result = await db.execute(
                    select(MockEnvironment.id).where(
                        MockEnvironment.id == preferred_env_id,
                        MockEnvironment.release_id == job.release_id,
                    )
                )
                if me_result.scalar_one_or_none() is None:
                    preferred_env_id = None
            target = await get_or_create_build_target(
                db,
                job.release_id,
                normalize_machine_arch(target_arch),
                preferred_env_id,
            )
            job.target_id = target.id
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
    affected_worker_ids: set[int] = set()
    for att in att_result.scalars().all():
        att.status = BuildStatus.CANCELLED
        att.finished_at = now
        att.error_message = "Cancelled by user"
        if att.signing_status in ("not_started", "running", "uploading"):
            att.signing_status = "failed"
        if att.worker_id is not None:
            affected_worker_ids.add(att.worker_id)
    # Without this, the worker row stays BUSY with current_build_attempt_id pointing at a
    # cancelled attempt until its next /poll heals itself, which blocks capacity accounting
    # in the UI and any logic that keys off worker.state.
    if affected_worker_ids:
        await db.flush()
        from oreon_build.services.worker_state_sync import (
            sync_worker_state_after_attempt_finished,
        )
        for wid in affected_worker_ids:
            w = await db.get(Worker, wid)
            if w is not None:
                await sync_worker_state_after_attempt_finished(db, w)
    await log_audit(
        db,
        "build.cancel",
        account_id=user.id,
        resource_type="build_job",
        resource_id=str(job_id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    from oreon_build.services.build_chain import on_user_cancelled_chain_job

    try:
        await on_user_cancelled_chain_job(db, job)
    except Exception:
        logger.exception("chain cancel side effect job_id=%s", job_id)
    payload = await _job_to_response(db, job)
    return JSONResponse(status_code=200, content=payload.model_dump(mode="json"))
