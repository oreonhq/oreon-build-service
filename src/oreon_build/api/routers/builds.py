import logging
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse

from oreon_build.utils.spec import name_from_srpm_filename, parse_spec_content
from sqlalchemy import String, cast, func, inspect as sa_inspect, or_, select
from sqlalchemy.orm import selectinload

from oreon_build.api.deps import CurrentAdmin, CurrentUser, DbSession
from oreon_build.api.schemas import (
    ArtifactResponse,
    BuildAttemptResponse,
    BuildJobResponse,
    BuildTrigger,
    BuildTriggerAccepted,
    CILayerTrigger,
    CILayerTriggerResponse,
    ChainBuildTrigger,
    ChainPlanStatusResponse,
    LayerBuildTrigger,
    LayerBuildTriggerResponse,
    LayerJobRow,
    LayerPlanStatusResponse,
    LayerPlanSummary,
    CurrentFailureItem,
    DistgitBatchTrigger,
    PaginatedResponse,
    RpmbuildPlanCreate,
    RpmbuildPlanResponse,
)
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import BuildAttempt, LayerPlan, LayerPlanStatus, BuildJob, BuildStatus, BuildTarget, Package, Release, RpmbuildPlan, Worker
from oreon_build.services.build_target_util import (
    get_or_create_build_target,
    has_successful_build_for_target,
    latest_package_version_id,
)
from oreon_build.services.r2 import get_r2_client, upload_r2_key

router = APIRouter(prefix="/builds", tags=["builds"])

logger = logging.getLogger(__name__)

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
    raw = _normalize_signing_status((a.signing_status or "").strip() or "not_started")
    if raw != "not_started":
        return raw
    if a.status == BuildStatus.SUCCESS:
        return "signed"
    if a.status in (BuildStatus.FAILED, BuildStatus.CANCELLED, BuildStatus.SKIPPED):
        return "failed"
    return "not_started"


def _effective_signing_status_job(job: BuildJob, latest: BuildAttempt | None) -> str | None:
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
async def list_current_failures(
    db: DbSession,
    layer_plan_only: bool = Query(False, description="Only jobs that were part of a layer plan"),
):
    from oreon_build.services.current_build_failures import fetch_current_failure_rows

    rows = await fetch_current_failure_rows(db, layer_plan_only=layer_plan_only)
    out: list[CurrentFailureItem] = []
    for row in rows:
        job = row.job
        att = row.latest_attempt
        out.append(
            CurrentFailureItem(
                id=job.id,
                package_id=job.package_id,
                package_name=row.package_name,
                release_id=job.release_id,
                release_name=row.release_name,
                target_id=job.target_id,
                architecture=row.architecture,
                package_version_id=job.package_version_id,
                status=job.status.value,
                priority=job.priority,
                created_at=job.created_at,
                completed_at=job.completed_at,
                layer_plan_id=job.layer_plan_id,
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
    layer_plan_id: int | None = None,
    side_tag_id: int | None = None,
    status: str | None = None,
    search: str | None = Query(None, max_length=200, description="Filter by job id, package name, release name, arch, status"),
    limit: int = Query(25, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    q = select(BuildJob).options(
        selectinload(BuildJob.build_target),
        selectinload(BuildJob.package),
        selectinload(BuildJob.release),
        selectinload(BuildJob.side_tag),
    ).order_by(BuildJob.created_at.desc())
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
    if layer_plan_id is not None:
        q = q.where(BuildJob.layer_plan_id == layer_plan_id)
        count_q = count_q.where(BuildJob.layer_plan_id == layer_plan_id)
    if side_tag_id is not None:
        q = q.where(BuildJob.side_tag_id == side_tag_id)
        count_q = count_q.where(BuildJob.side_tag_id == side_tag_id)
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
    result = await db.execute(
        select(BuildJob).where(BuildJob.id == job_id).with_for_update()
    )
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


def _source_url_from_new_package(np) -> str | None:
    if np.source_url:
        return np.source_url
    if np.source_urls:
        return np.source_urls[0]
    return None


def _infer_github_html_url_from_new_package(np) -> str | None:
    u = _source_url_from_new_package(np)
    if not u or "github.com" not in u.lower():
        return None
    base = u.split("#", 1)[0].strip().rstrip("/")
    if base.lower().endswith(".git"):
        base = base[:-4]
    if not base.lower().startswith("http"):
        return None
    return base


@router.post("/trigger", status_code=status.HTTP_201_CREATED)
async def trigger_build(
    data: BuildTrigger,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    background: bool = Query(
        True,
        description="If true, return immediately and queue jobs in the background.",
    ),
):
    from oreon_build.services.build_trigger_bg import run_single_trigger, spawn
    from oreon_build.services.build_trigger_exec import execute_build_trigger

    release = await db.get(Release, data.release_id)
    if release is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    if not release.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Release is inactive")

    if background:
        await db.commit()
        spawn(
            run_single_trigger(
                data.model_dump(mode="json"),
                user.id,
                get_client_ip(request),
                get_user_agent(request),
            )
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=BuildTriggerAccepted(
                status="accepted",
                message="Build trigger accepted. Jobs are being queued in the background.",
            ).model_dump(mode="json"),
        )

    jobs_created = await execute_build_trigger(db, user, data, request=request)
    att_map = await _latest_attempt_by_job_ids(db, [j.id for j in jobs_created])
    return [await _job_to_response(db, j, att_map.get(j.id)) for j in jobs_created]


@router.post("/trigger-batch", status_code=status.HTTP_202_ACCEPTED, response_model=BuildTriggerAccepted)
async def trigger_distgit_batch(
    data: DistgitBatchTrigger,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from oreon_build.services.build_trigger_bg import run_distgit_batch, spawn

    names = sorted({(p.name or "").strip() for p in data.packages if (p.name or "").strip()})
    if not names:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No package names provided")
    rr = await db.execute(select(Release).where(Release.id == data.release_id))
    release = rr.scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    if not release.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Release is inactive")
    payload = data.model_dump(mode="json")
    await db.commit()
    spawn(
        run_distgit_batch(
            payload,
            user.id,
            get_client_ip(request),
            get_user_agent(request),
        )
    )
    return BuildTriggerAccepted(
        status="accepted",
        message="DistGit batch accepted. Packages are being queued in the background.",
        package_count=len(data.packages),
    )


@router.post("/layer-trigger", response_model=LayerBuildTriggerResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_layer_build_endpoint(
    data: LayerBuildTrigger,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from oreon_build.services.build_trigger_bg import run_layer_seed, spawn
    from oreon_build.services.layer_engine import create_pending_layer_plan

    roots = [str(p).strip() for p in data.packages if str(p).strip()]
    if not roots:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No packages given")

    plan = await create_pending_layer_plan(
        db,
        request,
        user,
        root_packages=roots,
        release_id=int(data.release_id),
        side_tag_id=int(data.side_tag_id),
        architectures=list(data.architectures or ["x86_64", "aarch64"]),
        preferred_mock_environment_id=data.preferred_mock_environment_id,
        fallback_mock_environment_id=data.fallback_mock_environment_id,
        distgit_url=data.distgit_url,
        distgit_branch=data.distgit_branch,
        priority=int(data.priority or 0),
    )
    await db.commit()
    spawn(run_layer_seed(plan.id))
    return LayerBuildTriggerResponse(
        layer_plan_id=plan.id,
        layer_plan_status=plan.status.value,
        root_packages=roots,
        jobs=[],
        message=(
            f"Layer {plan.id} accepted. Dependency seeding is running in the background "
            "and jobs will appear as soon as it's done."
        ),
    )


@router.post("/ci-layer-trigger", response_model=CILayerTriggerResponse, status_code=status.HTTP_202_ACCEPTED)
async def ci_layer_trigger_endpoint(
    data: CILayerTrigger,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from oreon_build.core.repo_branch import release_distgit_branch
    from oreon_build.services.build_trigger_bg import run_layer_seed, spawn
    from oreon_build.services.layer_engine import create_pending_layer_plan

    rel_result = await db.execute(select(Release).where(Release.id == data.release_id))
    release = rel_result.scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")

    distgit_url = data.distgit_url or "https://github.com/oreonhq/rpm-specfiles.git"
    distgit_branch = (data.distgit_branch or release_distgit_branch(release)).strip()
    architectures = list(data.architectures or ["x86_64", "aarch64"])
    root_packages = [str(p).strip() for p in data.packages if str(p).strip()]
    release_id = int(data.release_id)
    priority = int(data.priority or 0)

    if not root_packages:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No packages provided")

    from oreon_build.models import SideTagCreatedFrom
    from oreon_build.services.side_tags import ensure_side_tag

    if data.side_tag_id is not None:
        side_tag_id = int(data.side_tag_id)
    else:
        tag = await ensure_side_tag(
            db,
            release_id=release_id,
            name=distgit_branch,
            owner_account_id=user.id,
            created_from=SideTagCreatedFrom.GITHUB_PR,
        )
        side_tag_id = tag.id

    plan = await create_pending_layer_plan(
        db,
        request,
        user,
        root_packages=root_packages,
        release_id=release_id,
        side_tag_id=side_tag_id,
        architectures=architectures,
        preferred_mock_environment_id=None,
        fallback_mock_environment_id=None,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        priority=priority,
    )
    await db.commit()
    spawn(run_layer_seed(plan.id))
    return CILayerTriggerResponse(
        layer_plan_id=plan.id,
        layer_plan_status=plan.status.value,
        root_packages=root_packages,
        accepted=True,
        message=f"CI layer {plan.id} accepted. Seeding dependencies in the background.",
    )


@router.get("/layer", response_model=list[LayerPlanSummary])
async def list_layer_plans(
    db: DbSession,
    release_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    from oreon_build.models import SideTag

    stmt = select(LayerPlan).order_by(LayerPlan.id.desc()).limit(limit)
    if release_id is not None:
        stmt = stmt.where(LayerPlan.release_id == release_id)
    plans = list((await db.execute(stmt)).scalars().all())
    if not plans:
        return []
    plan_ids = [p.id for p in plans]
    counts_r = await db.execute(
        select(BuildJob.layer_plan_id, BuildJob.status, func.count(BuildJob.id))
        .where(BuildJob.layer_plan_id.in_(plan_ids))
        .group_by(BuildJob.layer_plan_id, BuildJob.status)
    )
    counts_by_plan: dict[int, dict[str, int]] = {}
    for pid, st, n in counts_r.all():
        counts_by_plan.setdefault(int(pid), {})[st.value] = int(n)

    release_ids = {p.release_id for p in plans}
    releases_r = await db.execute(select(Release).where(Release.id.in_(release_ids)))
    releases_by_id = {r.id: r for r in releases_r.scalars().all()}
    side_tag_ids = {p.side_tag_id for p in plans if p.side_tag_id}
    side_tags_by_id = {}
    if side_tag_ids:
        st_r = await db.execute(select(SideTag).where(SideTag.id.in_(side_tag_ids)))
        side_tags_by_id = {t.id: t for t in st_r.scalars().all()}

    out = []
    for p in plans:
        rel = releases_by_id.get(p.release_id)
        tag = side_tags_by_id.get(p.side_tag_id) if p.side_tag_id else None
        out.append(
            LayerPlanSummary(
                layer_plan_id=p.id,
                status=p.status.value,
                root_packages=list(p.root_packages or []),
                releasename=(rel.releasename if rel else None),
                side_tag_name=(tag.name if tag else None),
                counts=counts_by_plan.get(p.id, {}),
                created_at=p.created_at,
                updated_at=p.updated_at,
            )
        )
    return out


async def _layer_plan_to_status_response(db: DbSession, plan) -> LayerPlanStatusResponse:
    expired = sa_inspect(plan).expired_attributes
    if expired:
        await db.refresh(plan)
    rel = await db.get(Release, plan.release_id)
    from oreon_build.models import SideTag
    from sqlalchemy import func

    counts_r = await db.execute(
        select(BuildJob.status, func.count(BuildJob.id))
        .where(BuildJob.layer_plan_id == plan.id)
        .group_by(BuildJob.status)
    )
    counts: dict[str, int] = {st.value: int(n) for st, n in counts_r.all()}

    rows: list[LayerJobRow] = []
    if plan.status in (LayerPlanStatus.FAILED, LayerPlanStatus.CANCELLED, LayerPlanStatus.SUCCESS):
        jobs_r = await db.execute(
            select(BuildJob)
            .options(selectinload(BuildJob.package), selectinload(BuildJob.build_target))
            .where(
                BuildJob.layer_plan_id == plan.id,
                BuildJob.status == BuildStatus.FAILED,
            )
            .order_by(BuildJob.layer_index.asc().nulls_last(), BuildJob.id.asc())
            .limit(50)
        )
        jobs = list(jobs_r.scalars().all())
        rows = [
            LayerJobRow(
                job_id=j.id,
                package_name=j.package.name if j.package else "?",
                status=j.status.value,
                layer_index=j.layer_index,
                blocked_reason=None,
                architecture=j.build_target.architecture if j.build_target else None,
                blocked_on=[],
            )
            for j in jobs
        ]

    side_tag = await db.get(SideTag, plan.side_tag_id) if plan.side_tag_id else None
    return LayerPlanStatusResponse(
        layer_plan_id=plan.id,
        status=plan.status.value,
        root_packages=list(plan.root_packages or []),
        jobs=rows,
        counts=counts,
        error_message=plan.error_message,
        releasename=(rel.releasename if rel else None),
        side_tag_name=(side_tag.name if side_tag else None),
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


@router.get("/layer/{layer_plan_id}/status", response_model=LayerPlanStatusResponse)
async def get_layer_plan_status(layer_plan_id: int, db: DbSession):
    plan = await db.get(LayerPlan, layer_plan_id)
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layer plan not found")
    return await _layer_plan_to_status_response(db, plan)


@router.post("/layer/{layer_plan_id}/cancel", response_model=LayerPlanStatusResponse)
async def cancel_layer_build_endpoint(
    layer_plan_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from oreon_build.services.layer_plan import cancel_layer_plan_by_user

    plan = await cancel_layer_plan_by_user(db, request, user, layer_plan_id=layer_plan_id)
    await db.commit()
    await db.refresh(plan)
    return await _layer_plan_to_status_response(db, plan)


@router.post("/layer/{layer_plan_id}/resume", response_model=LayerPlanStatusResponse)
async def resume_layer_build_endpoint(
    layer_plan_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from oreon_build.services.build_trigger_bg import schedule_layer_resume_refresh
    from oreon_build.services.layer_plan import resume_layer_plan_by_user

    plan, reopen_ids = await resume_layer_plan_by_user(
        db, request, user, layer_plan_id=layer_plan_id
    )
    await db.commit()
    await db.refresh(plan)
    response = await _layer_plan_to_status_response(db, plan)
    schedule_layer_resume_refresh(int(plan.id), reopen_ids)
    return response


@router.post("/mass-rebuild/run", response_model=dict)
async def mass_rebuild_run_once(
    db: DbSession,
    admin: CurrentAdmin,
):
    from oreon_build.services.mass_rebuild import run_mass_rebuild_pass

    return await run_mass_rebuild_pass(db)


@router.post("/chain-trigger", response_model=RpmbuildPlanResponse, status_code=status.HTTP_201_CREATED)
async def trigger_chain_build_endpoint(
    data: ChainBuildTrigger,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    from oreon_build.services.rpmbuild_order import create_manual_chain_plan
    from oreon_build.services.side_tags import get_side_tag, require_side_tag_access

    tag = await get_side_tag(db, data.side_tag_id)
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Side tag not found")
    require_side_tag_access(user, tag)
    out = await create_manual_chain_plan(
        db,
        request,
        chain=data.chain,
        release_id=data.release_id,
        side_tag_id=data.side_tag_id,
        architectures=data.architectures,
        priority=data.priority,
        preferred_mock_environment_id=data.preferred_mock_environment_id,
        distgit_url=data.distgit_url,
        distgit_branch=data.distgit_branch,
        triggered_by_id=user.id,
    )
    await db.commit()
    await db.refresh(out.plan)
    return RpmbuildPlanResponse(
        id=out.plan.id,
        release_id=out.plan.release_id,
        releasename=out.plan.releasename,
        repo_branch=out.plan.repo_branch,
        distgit_url=out.plan.distgit_url,
        distgit_branch=out.plan.distgit_branch,
        seed_mode=out.plan.seed_mode,
        seed_packages=list(out.plan.seed_packages_json or []),
        status=out.plan.status.value,
        current_open_layer=int(out.plan.current_open_layer or 0),
        total_layers=len(out.layers),
        queued_jobs=len(out.jobs),
        created_at=out.plan.created_at,
        updated_at=out.plan.updated_at,
    )


async def _chain_plan_to_status_response(db: DbSession, plan: RpmbuildPlan) -> ChainPlanStatusResponse:
    from oreon_build.models import SideTag

    groups = plan.layers_json if isinstance(plan.layers_json, list) else []
    counts_r = await db.execute(
        select(BuildJob.status, func.count(BuildJob.id))
        .where(BuildJob.rpmbuild_plan_id == plan.id)
        .group_by(BuildJob.status)
    )
    counts = {st.value: int(n) for st, n in counts_r.all()}
    jobs_r = await db.execute(
        select(BuildJob)
        .options(selectinload(BuildJob.package), selectinload(BuildJob.build_target))
        .where(
            BuildJob.rpmbuild_plan_id == plan.id,
            BuildJob.status == BuildStatus.FAILED,
        )
        .order_by(BuildJob.rpmbuild_layer_index.asc().nulls_last(), BuildJob.id.asc())
        .limit(100)
    )
    rows = [
        LayerJobRow(
            job_id=j.id,
            package_name=j.package.name if j.package else "?",
            status=j.status.value,
            layer_index=j.rpmbuild_layer_index,
            architecture=j.build_target.architecture if j.build_target else None,
            blocked_reason=j.error_message,
            blocked_on=[],
        )
        for j in jobs_r.scalars().all()
    ]
    rel = await db.get(Release, plan.release_id)
    tag = await db.scalar(select(SideTag).where(SideTag.release_id == plan.release_id, SideTag.name == plan.repo_branch))
    return ChainPlanStatusResponse(
        chain_plan_id=plan.id,
        status=plan.status.value,
        groups=groups,
        current_group=int(plan.current_open_layer or 0) + 1,
        total_groups=len(groups),
        jobs=rows,
        counts=counts,
        error_message=plan.error_message,
        releasename=rel.releasename if rel else None,
        side_tag_name=tag.name if tag else None,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


@router.get("/chain/{chain_plan_id}/status", response_model=ChainPlanStatusResponse)
async def get_chain_build_status(chain_plan_id: int, db: DbSession):
    plan = await db.get(RpmbuildPlan, chain_plan_id)
    if plan is None or plan.seed_mode != "manual_chain":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chain build not found")
    return await _chain_plan_to_status_response(db, plan)


@router.post("/chain/{chain_plan_id}/cancel", response_model=ChainPlanStatusResponse)
async def cancel_chain_build_endpoint(
    chain_plan_id: int, request: Request, db: DbSession, user: CurrentUser,
):
    from oreon_build.services.rpmbuild_settle import cancel_chain_plan_by_user

    plan = await cancel_chain_plan_by_user(db, request, user, chain_plan_id)
    await db.commit()
    await db.refresh(plan)
    return await _chain_plan_to_status_response(db, plan)


@router.post("/chain/{chain_plan_id}/resume", response_model=ChainPlanStatusResponse)
async def resume_chain_build_endpoint(
    chain_plan_id: int, request: Request, db: DbSession, user: CurrentUser,
):
    from oreon_build.services.rpmbuild_settle import resume_chain_plan_by_user

    plan = await resume_chain_plan_by_user(db, request, user, chain_plan_id)
    await db.commit()
    await db.refresh(plan)
    return await _chain_plan_to_status_response(db, plan)


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
    side_tag_id: int = Form(...),
    preferred_mock_environment_id: int | None = Form(None),
    architectures: str = Form("x86_64,aarch64", description="Comma-separated, e.g. x86_64,aarch64"),
    files: list[UploadFile] = File(...),
):
    from datetime import datetime, timezone
    from oreon_build.models import BuildAttempt, MockEnvironment, Package, PackageVersion, Release, Source
    from oreon_build.services.side_tags import bump_side_tag_activity, resolve_required_side_tag

    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one file (SRPM or .spec) required")
    for f in files:
        fn = (f.filename or "").lower()
        if not any(fn.endswith(ext) for ext in ALLOWED_UPLOAD_EXTENSIONS):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File must be .src.rpm or .spec; got {f.filename}",
            )

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
    if not release.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Release is inactive")
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

    side_tag = await resolve_required_side_tag(
        db, release_id=release_id, side_tag_id=side_tag_id, user=user, require_access=True
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
            continue
        target = await get_or_create_build_target(db, release_id, canon, mock_env_id)
        job = BuildJob(
            package_id=pkg.id,
            release_id=release_id,
            target_id=target.id,
            package_version_id=pv.id,
            status=BuildStatus.PENDING,
            priority=0,
            triggered_by_id=user.id,
            branch=side_tag.name,
            side_tag_id=side_tag.id,
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
    await bump_side_tag_activity(db, side_tag.id)
    first = jobs_created[0]
    payload = await _job_to_response(db, first)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=payload.model_dump(mode="json"),
    )


async def _job_to_response(
    db: DbSession, job: BuildJob, latest: BuildAttempt | None = None
) -> BuildJobResponse:
    if latest is None:
        r = await db.execute(
            select(BuildAttempt)
            .where(BuildAttempt.build_job_id == job.id)
            .order_by(BuildAttempt.id.desc())
            .limit(1)
        )
        latest = r.scalar_one_or_none()
    insp = sa_inspect(job)
    arch = None
    if "build_target" not in insp.unloaded and job.build_target is not None:
        arch = job.build_target.architecture
    elif job.target_id:
        r = await db.execute(select(BuildTarget.architecture).where(BuildTarget.id == job.target_id))
        arch = r.scalars().one_or_none()
    package_name = None
    if "package" not in insp.unloaded and job.package is not None:
        package_name = job.package.name
    if not package_name:
        package_name = await db.scalar(select(Package.name).where(Package.id == job.package_id))
    release_name = None
    if "release" not in insp.unloaded and job.release is not None:
        release_name = job.release.releasename
    if not release_name:
        release_name = await db.scalar(select(Release.releasename).where(Release.id == job.release_id))
    side_tag_name = None
    st_id = getattr(job, "side_tag_id", None)
    if "side_tag" not in insp.unloaded and job.side_tag is not None:
        side_tag_name = job.side_tag.name
    if st_id and not side_tag_name:
        from oreon_build.models import SideTag

        side_tag_name = await db.scalar(select(SideTag.name).where(SideTag.id == st_id))
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
        layer_plan_id=getattr(job, "layer_plan_id", None),
        side_tag_id=st_id,
        side_tag_name=side_tag_name,
        branch=getattr(job, "branch", None),
        layer_index=getattr(job, "layer_index", None),
        blocked_reason=getattr(job, "blocked_reason", None),
        rpmbuild_plan_id=getattr(job, "rpmbuild_plan_id", None),
        rpmbuild_layer_index=getattr(job, "rpmbuild_layer_index", None),
    )


@router.post("/jobs/{job_id}/retry")
async def retry_build(
    job_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    force: bool = Query(False),
):
    result = await db.execute(select(BuildJob).where(BuildJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build job not found")
    if job.status in (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING, BuildStatus.BLOCKED):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Job is already active")
    live_attempt = await db.scalar(
        select(BuildAttempt.id)
        .where(
            BuildAttempt.build_job_id == job.id,
            BuildAttempt.status.in_(
                (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING)
            ),
        )
        .limit(1)
    )
    if live_attempt is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job still has active attempt {live_attempt}",
        )
    latest_attempt = await db.scalar(
        select(BuildAttempt)
        .where(BuildAttempt.build_job_id == job.id)
        .order_by(BuildAttempt.id.desc())
        .limit(1)
    )
    from oreon_build.services.retry_eligibility import source_failure_can_retry

    retryable, retry_detail = await source_failure_can_retry(
        db, job, latest_attempt, force=force
    )
    if not retryable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This job cannot be retried unchanged because {retry_detail}.",
        )

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
    if job.layer_plan_id is not None:
        job.status = BuildStatus.BLOCKED
        job.blocked_reason = "waiting on repository"
        job.blocking_sources = []
        job.error_message = None
        plan = await db.get(LayerPlan, job.layer_plan_id)
        if plan is not None and plan.status in (LayerPlanStatus.FAILED, LayerPlanStatus.CANCELLED):
            plan.status = LayerPlanStatus.RUNNING
            plan.error_message = None
        from oreon_build.services.build_trigger_bg import schedule_layer_resume_refresh

        resume_plan_id = int(job.layer_plan_id)
    else:
        resume_plan_id = None
    await log_audit(
        db,
        "build.retry",
        account_id=user.id,
        resource_type="build_job",
        resource_id=str(job_id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        details=retry_detail or "",
    )
    await db.commit()
    if resume_plan_id is not None:
        schedule_layer_resume_refresh(resume_plan_id, [int(job.id)])
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
    from oreon_build.services.layer_plan import on_user_cancelled_layer_job

    try:
        await on_user_cancelled_layer_job(db, job)
    except Exception:
        logger.exception("layer cancel side effect job_id=%s", job_id)
    payload = await _job_to_response(db, job)
    return JSONResponse(status_code=200, content=payload.model_dump(mode="json"))
