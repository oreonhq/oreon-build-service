from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oreon_build.services.build_target_util import get_or_create_build_target
from oreon_build.config import get_settings
from oreon_build.core.audit import log_audit
from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.models import BuildJob, BuildStatus, Package, PackageVersion, Release, Source
from oreon_build.services.github import get_github_client

logger = logging.getLogger(__name__)

TriggerKind = Literal["webhook", "catchup"]

GITHUB_PUSH_EMPTY_DIFF_SENTINEL = "__oreon_empty_diff__"

GITHUB_COMPARE_MAX_FILES = 300

def normalize_repo_url(url: str | None) -> str:
    if not url:
        return ""
    u = url.strip().rstrip("/").lower()
    if u.endswith(".git"):
        u = u[:-4]
    if u.startswith("git@"):
        parts = u.split(":", 1)
        if len(parts) == 2:
            host_path = parts[1].replace(".git", "").lower()
            if host_path.startswith("github.com/"):
                return host_path
            return "github.com/" + host_path if "/" in host_path else host_path
    try:
        p = urlparse(u)
        if p.netloc and p.path:
            return f"{p.netloc}{p.path.rstrip('/').lower()}"
    except Exception:
        pass
    return u.lower()

def package_matches_github_repo(pkg: Package, repository: dict[str, Any], repo_id: int) -> bool:
    if pkg.github_repository_id is not None and int(pkg.github_repository_id) == int(repo_id):
        return True
    fn = (repository.get("full_name") or "").strip().lower()
    if fn and pkg.github_html_url:
        w = pkg.github_html_url.lower().replace(".git", "")
        if fn in w:
            return True
    hook_urls = [
        normalize_repo_url(repository.get("html_url")),
        normalize_repo_url(repository.get("clone_url")),
        normalize_repo_url(repository.get("ssh_url")),
    ]
    pu = normalize_repo_url(pkg.github_html_url)
    if pu and pu in {h for h in hook_urls if h}:
        return True
    if fn:
        needle = f"github.com/{fn}".lower()
        for pv in pkg.versions or []:
            for src in pv.sources or []:
                u = (src.url or "").lower().replace(".git", "")
                if u and needle in u:
                    return True
    return False

def parse_distgit_branch_and_spec(url: str) -> tuple[str | None, str | None]:
    if "#" not in url:
        return None, None
    _, frag = url.split("#", 1)
    frag = frag.strip()
    if not frag:
        return None, None
    if ":" in frag:
        br, spec = frag.split(":", 1)
        return (br.strip() or None, (spec.strip().lstrip("/").replace("\\", "/") or None))
    return frag, None

def latest_distgit_source(pkg: Package) -> tuple[str | None, str | None]:
    for pv in pkg.versions or []:
        for src in pv.sources or []:
            if not src.url or src.kind not in ("distgit", "scm"):
                continue
            br, spec = parse_distgit_branch_and_spec(src.url)
            if spec:
                return br, spec
    return None, None

def push_touches_distgit_spec(changed: set[str], spec_rel: str | None) -> bool:
    if not spec_rel:
        return False
    if GITHUB_PUSH_EMPTY_DIFF_SENTINEL in changed:
        return False
    if not changed:
        return True
    spec_n = spec_rel.replace("\\", "/").lstrip("/")
    changed_n = {p.lstrip("/") for p in changed}
    if spec_n in changed_n:
        return True
    parent = str(Path(spec_n).parent)
    if parent in (".", ""):
        return False
    prefix = parent + "/"
    for p in changed_n:
        if p == spec_n or p.startswith(prefix):
            return True
    for p in changed_n:
        if not p or p.count("/") < 1:
            continue
        if spec_n.endswith(p):
            return True
    return False

def candidate_package_names_from_paths(changed: set[str]) -> set[str]:
    names: set[str] = set()
    for raw in changed:
        p = (raw or "").strip().replace("\\", "/").lstrip("/")
        if not p:
            continue
        parts = [x for x in p.split("/") if x]
        if not parts:
            continue
        base = parts[-1]
        if base.endswith(".spec"):
            stem = base[:-5]
            if stem:
                names.add(stem)
        if len(parts) >= 2:
            names.add(parts[-2])
    return {n for n in names if n}

async def release_arch_combos_for_auto_build(db: AsyncSession, pkg: Package) -> set[tuple[int, str]]:
    combos: set[tuple[int, str]] = set()
    for j in pkg.build_jobs or []:
        if j.target_id and j.build_target:
            combos.add((j.release_id, j.build_target.architecture))
    if combos:
        return combos
    r = await db.execute(select(Release.id).where(Release.is_active.is_(True)))
    for rid in r.scalars().all():
        for arch in ("x86_64", "aarch64"):
            combos.add((rid, arch))
    return combos

def _paths_from_commit_object(c: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("added", "modified"):
        for p in c.get(key) or []:
            if isinstance(p, str) and p.strip():
                out.add(p.strip().replace("\\", "/"))
    return out

def collect_paths_from_webhook_commits(body: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for c in body.get("commits") or []:
        if isinstance(c, dict):
            out |= _paths_from_commit_object(c)
    hc = body.get("head_commit")
    if isinstance(hc, dict):
        out |= _paths_from_commit_object(hc)
    return out

def push_webhook_ref_advanced(body: dict[str, Any]) -> bool:
    after = body.get("after")
    if not isinstance(after, str) or not after.strip():
        return False
    after_s = after.strip()
    before = body.get("before")
    if _is_null_github_sha(before if isinstance(before, str) else None):
        return True
    return isinstance(before, str) and bool(before.strip()) and before.strip() != after_s

def _is_null_github_sha(sha: str | None) -> bool:
    if not sha or not isinstance(sha, str):
        return True
    s = sha.strip().lower()
    return len(s) == 40 and set(s) <= {"0"}

async def resolve_github_webhook_changed_paths(
    body: dict[str, Any],
    repository: dict[str, Any],
) -> set[str]:
    from_commits = collect_paths_from_webhook_commits(body)
    settings = get_settings()
    if not settings.github_token:
        return from_commits

    full_name = (repository.get("full_name") or "").strip()
    if "/" not in full_name:
        return from_commits

    owner, repo_name = full_name.split("/", 1)
    after = body.get("after")
    if not isinstance(after, str) or not after.strip():
        return from_commits
    after_sha = after.strip()

    before = body.get("before")
    if _is_null_github_sha(before if isinstance(before, str) else None):
        base = (repository.get("default_branch") or "main").strip() or "main"
        head = after_sha
    elif isinstance(before, str) and before.strip():
        base = before.strip()
        head = after_sha
    else:
        return from_commits

    client = get_github_client()
    try:
        cmp = await client.compare_refs(owner, repo_name, base, head)
        if cmp is None:
            logger.warning(
                "GitHub webhook compare failed repo=%s base=%s head=%s (using commit paths only)",
                full_name,
                base[:7] if len(base) >= 7 else base,
                head[:7],
            )
            return from_commits
        files = cmp.get("files") or []
        api_paths = collect_paths_from_compare_files(cmp)
        if cmp.get("truncated") or len(files) >= GITHUB_COMPARE_MAX_FILES:
            logger.warning(
                "GitHub webhook compare truncated/capped repo=%s files=%s paths=%s, "
                "triggering only matched packages (catch-up handles the rest)",
                full_name,
                len(files),
                len(api_paths),
            )
            if not api_paths and not from_commits:
                logger.warning(
                    "GitHub webhook compare returned no paths at all repo=%s, auto-triggering all distgit packages",
                    full_name,
                )
                return set()
        merged = from_commits | api_paths
        ahead = int(cmp.get("ahead_by") or 0)
        if not merged:
            if ahead > 0:
                logger.warning(
                    "GitHub webhook compare ahead_by=%s but zero file paths repo=%s, auto-triggering all distgit packages",
                    ahead,
                    full_name,
                )
                return set()
            if push_webhook_ref_advanced(body):
                logger.warning(
                    "GitHub webhook has zero resolved paths but the ref advanced repo=%s, auto-triggering all distgit packages",
                    full_name,
                )
                return set()
            return {GITHUB_PUSH_EMPTY_DIFF_SENTINEL}
        return merged
    finally:
        await client.close()

def collect_paths_from_compare_files(compare_json: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for f in compare_json.get("files") or []:
        if not isinstance(f, dict):
            continue
        status = (f.get("status") or "").lower()
        if status == "removed":
            continue
        fn = f.get("filename")
        if isinstance(fn, str) and fn.strip():
            out.add(fn.strip().replace("\\", "/"))
        if status == "renamed":
            prev = f.get("previous_filename")
            if isinstance(prev, str) and prev.strip():
                out.add(prev.strip().replace("\\", "/"))
    return out

async def load_matching_packages_for_github_repo(
    db: AsyncSession,
    repository: dict[str, Any],
    repo_id: int,
) -> list[Package]:
    conds: list[Any] = [Package.github_repository_id == repo_id]
    fn = (repository.get("full_name") or "").strip()
    if fn:
        conds.append(Package.github_html_url.ilike(f"%{fn}%"))
        like_http = f"%github.com/{fn}%"
        like_ssh = f"%github.com:{fn}%"
        src_exists = exists(
            select(1)
            .select_from(PackageVersion)
            .join(Source, Source.package_version_id == PackageVersion.id)
            .where(
                PackageVersion.package_id == Package.id,
                Source.url.isnot(None),
                or_(Source.url.ilike(like_http), Source.url.ilike(like_ssh)),
            )
        )
        conds.append(src_exists)
    pkg_result = await db.execute(
        select(Package)
        .where(or_(*conds))
        .options(
            selectinload(Package.build_jobs).selectinload(BuildJob.build_target),
            selectinload(Package.versions).selectinload(PackageVersion.sources),
        )
    )
    candidates = list(pkg_result.scalars().all())
    return [p for p in candidates if package_matches_github_repo(p, repository, repo_id)]

async def apply_github_push(
    db: AsyncSession,
    repository: dict[str, Any],
    repo_id: int,
    push_branch: str | None,
    changed_paths: set[str],
    *,
    kind: TriggerKind,
    max_jobs: int | None = None,
) -> dict[str, Any]:
    fn = (repository.get("full_name") or "").strip()
    packages = await load_matching_packages_for_github_repo(db, repository, repo_id)
    if not packages:
        logger.info(
            "GitHub push no_matching_package repo=%s repo_id=%s branch=%s (%s)",
            fn or "?",
            repo_id,
            push_branch,
            kind,
        )
        return {"status": "ignored", "reason": "no_matching_package", "jobs": [], "skipped": []}

    if changed_paths and GITHUB_PUSH_EMPTY_DIFF_SENTINEL not in changed_paths:
        name_candidates = {n.lower() for n in candidate_package_names_from_paths(changed_paths)}
        if name_candidates:
            narrowed = [p for p in packages if (p.name or "").strip().lower() in name_candidates]
            if narrowed:
                logger.info(
                    "GitHub push prefilter repo=%s repo_id=%s changed_paths=%s packages_before=%s packages_after=%s",
                    fn or "?",
                    repo_id,
                    len(changed_paths),
                    len(packages),
                    len(narrowed),
                )
                packages = narrowed
            else:
                logger.warning(
                    "GitHub push repo=%s changed_paths=%s names=%s — none registered in Oreon, no builds triggered "
                    "(register the package via DistGit trigger first, then future pushes will auto-build it)",
                    fn or "?",
                    len(changed_paths),
                    sorted(name_candidates)[:10],
                )
                return {"status": "ignored", "reason": "unregistered_packages", "jobs": [], "skipped": []}

    logger.info(
        "GitHub push changed_paths=%s sample=%s repo=%s kind=%s",
        len(changed_paths),
        sorted(changed_paths)[:10],
        fn or "?",
        kind,
    )

    audit_action = "build.github_auto_trigger" if kind == "webhook" else "build.github_catchup_trigger"
    user_agent = "github-webhook" if kind == "webhook" else "github-catchup"

    triggered: list[int] = []
    skipped: list[dict[str, Any]] = []

    for pkg in packages:
        dg_branch, spec_rel = latest_distgit_source(pkg)
        if not spec_rel:
            skipped.append({"package_id": pkg.id, "reason": "no_distgit_source"})
            continue
        if dg_branch and push_branch and dg_branch != push_branch:
            skipped.append(
                {
                    "package_id": pkg.id,
                    "reason": "branch_mismatch",
                    "distgit_branch": dg_branch,
                    "push_branch": push_branch,
                }
            )
            continue
        if not push_touches_distgit_spec(changed_paths, spec_rel):
            skipped.append({"package_id": pkg.id, "reason": "no_matching_paths", "spec": spec_rel})
            continue

        combos = await release_arch_combos_for_auto_build(db, pkg)
        if not combos:
            skipped.append({"package_id": pkg.id, "reason": "no_release_combo"})
            continue

        for release_id, arch in combos:
            if max_jobs is not None and len(triggered) >= max_jobs:
                skipped.append({"package_id": pkg.id, "reason": "max_jobs_limit_reached", "limit": max_jobs})
                break
            rel_row = await db.execute(select(Release).where(Release.id == release_id))
            rel_obj = rel_row.scalar_one_or_none()
            if not rel_obj:
                skipped.append({"package_id": pkg.id, "release_id": release_id, "reason": "release_not_found"})
                continue
            publish_branch = r2_publish_branch(rel_obj)
            target = await get_or_create_build_target(db, release_id, arch, pkg.preferred_mock_environment_id)
            job = BuildJob(
                package_id=pkg.id,
                release_id=release_id,
                target_id=target.id,
                package_version_id=None,
                status=BuildStatus.PENDING,
                priority=0,
                triggered_by_id=None,
                branch=publish_branch,
            )
            db.add(job)
            await db.flush()
            triggered.append(job.id)
            await log_audit(
                db,
                audit_action,
                account_id=None,
                resource_type="build_job",
                resource_id=str(job.id),
                details=f"package_id={pkg.id} release_id={release_id} arch={arch} repo_id={repo_id} branch={publish_branch}",
                ip_address=None,
                user_agent=user_agent,
                emit_discord=(kind == "webhook"),
            )

    logger.info(
        "GitHub push repo=%s repo_id=%s branch=%s kind=%s triggered=%s skipped=%s",
        fn or "?",
        repo_id,
        push_branch,
        kind,
        len(triggered),
        len(skipped),
    )
    if not triggered and skipped:
        logger.warning(
            "GitHub push matched packages but created no jobs repo=%s kind=%s skipped=%s",
            fn or "?",
            kind,
            skipped,
        )
    return {"status": "ok", "jobs": triggered, "skipped": skipped}
