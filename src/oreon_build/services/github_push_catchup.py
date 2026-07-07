from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oreon_build.config import get_settings
from oreon_build.models import GitHubPushCursor, Package, PackageVersion, Source
from oreon_build.services.github import GitHubClient, get_github_client
from oreon_build.services.github_push_trigger import (
    apply_github_push,
    collect_paths_from_compare_files,
    parse_distgit_branch_and_spec,
)

logger = logging.getLogger(__name__)

def _owner_repo_from_any_github_url(url: str) -> tuple[str, str] | None:
    base = url.split("#")[0].strip()
    if not base:
        return None
    if base.startswith("git@"):
        rest = base.split("@", 1)[-1]
        if ":" not in rest:
            return None
        host, _, pathpart = rest.partition(":")
        if "github.com" not in host.lower():
            return None
        path = pathpart.replace(".git", "")
        segs = [p for p in path.split("/") if p]
        if len(segs) >= 2:
            return segs[0], segs[1]
        return None
    p = urlparse(base)
    if "github.com" not in p.netloc.lower():
        return None
    segs = [x for x in p.path.strip("/").replace(".git", "").split("/") if x]
    if len(segs) >= 2:
        return segs[0], segs[1]
    return None

async def _resolve_repository_id(
    client: GitHubClient,
    owner: str,
    repo: str,
    cache: dict[tuple[str, str], int],
) -> int | None:
    key = (owner, repo)
    if key in cache:
        return cache[key]
    data = await client.get_repository(owner, repo)
    if not data or data.get("id") is None:
        return None
    rid = int(data["id"])
    cache[key] = rid
    return rid

async def discover_repo_branch_pairs(db: AsyncSession, client: GitHubClient) -> list[tuple[int, str]]:
    id_rows = await db.execute(
        select(Package.id)
        .join(PackageVersion, PackageVersion.package_id == Package.id)
        .join(Source, Source.package_version_id == PackageVersion.id)
        .where(
            Source.kind.in_(["distgit", "scm"]),
            Source.url.isnot(None),
            or_(
                Source.url.ilike("%github.com/%"),
                Source.url.ilike("%github.com:%"),
                Source.url.ilike("git@github.com:%"),
            ),
        )
        .distinct()
    )
    ids = [i for i in id_rows.scalars().all() if i is not None]
    if not ids:
        return []
    r = await db.execute(
        select(Package)
        .where(Package.id.in_(ids))
        .options(selectinload(Package.versions).selectinload(PackageVersion.sources))
    )
    packages = list(r.scalars().unique().all())

    resolv_cache: dict[tuple[str, str], int] = {}
    seen: set[tuple[int, str]] = set()
    out: list[tuple[int, str]] = []

    for pkg in packages:
        for pv in pkg.versions:
            for src in pv.sources:
                if src.kind not in ("distgit", "scm") or not src.url:
                    continue
                br, spec = parse_distgit_branch_and_spec(src.url)
                if not spec or not br:
                    continue
                repo_id: int | None = pkg.github_repository_id
                if repo_id is None:
                    own = _owner_repo_from_any_github_url(src.url)
                    if not own:
                        continue
                    repo_id = await _resolve_repository_id(client, own[0], own[1], resolv_cache)
                if repo_id is None:
                    continue
                key = (repo_id, br)
                if key in seen:
                    continue
                seen.add(key)
                out.append(key)
    return out

async def upsert_github_push_cursor(db: AsyncSession, repository_id: int, branch: str, head_sha: str) -> None:
    r = await db.execute(
        select(GitHubPushCursor).where(
            GitHubPushCursor.repository_id == repository_id,
            GitHubPushCursor.branch == branch,
        )
    )
    row = r.scalar_one_or_none()
    if row:
        row.head_sha = head_sha
    else:
        db.add(GitHubPushCursor(repository_id=repository_id, branch=branch, head_sha=head_sha))

async def run_github_push_catchup(db: AsyncSession) -> dict[str, Any]:
    settings = get_settings()
    if not settings.github_push_auto_build_enabled:
        logger.info("GitHub catch-up skipped (GITHUB_PUSH_AUTO_BUILD_ENABLED is false)")
        return {"status": "skipped", "reason": "github_push_auto_build_disabled"}
    if not settings.github_token:
        logger.info("GitHub catch-up skipped (no GITHUB_TOKEN)")
        return {"status": "skipped", "reason": "no_github_token"}

    summary: dict[str, Any] = {
        "pairs_checked": 0,
        "cursor_initialized_first_run": 0,
        "cursor_advanced_after_compare": 0,
        "first_run_backfill_compares": 0,
        "jobs": 0,
        "max_jobs_per_cycle": int(settings.github_catchup_max_jobs_per_cycle),
        "errors": [],
    }
    jobs_left = int(settings.github_catchup_max_jobs_per_cycle)

    client = get_github_client()
    try:
        pairs = await discover_repo_branch_pairs(db, client)
        summary["pairs_checked"] = len(pairs)

        for repository_id, branch in pairs:
            if jobs_left <= 0:
                logger.warning("GitHub catch-up job cap reached for this cycle limit=%s", summary["max_jobs_per_cycle"])
                break
            try:
                repo_json = await client.get_repository_by_id(repository_id)
                if not repo_json:
                    summary["errors"].append(f"repo_id={repository_id} not found")
                    continue
                full_name = (repo_json.get("full_name") or "").strip()
                if "/" not in full_name:
                    continue
                owner, repo_name = full_name.split("/", 1)

                commits = await client.list_commits_on_ref(owner, repo_name, branch, per_page=1)
                if not commits:
                    summary["errors"].append(f"{full_name}@{branch} no commits")
                    continue
                tip_sha = (commits[0].get("sha") or "").strip()
                if not tip_sha:
                    continue

                cur_r = await db.execute(
                    select(GitHubPushCursor).where(
                        GitHubPushCursor.repository_id == repository_id,
                        GitHubPushCursor.branch == branch,
                    )
                )
                cur = cur_r.scalar_one_or_none()
                if cur is None:
                    summary["cursor_initialized_first_run"] += 1
                    did_backfill = False
                    bootstrap_n = max(0, int(settings.github_catchup_bootstrap_commits or 0))
                    if bootstrap_n > 1 and jobs_left > 0:
                        boot = await client.list_commits_on_ref(owner, repo_name, branch, per_page=min(500, bootstrap_n))
                        if len(boot) > 1:
                            oldest = boot[-1] if isinstance(boot[-1], dict) else {}
                            parents = oldest.get("parents") or []
                            base_sha = ((parents[0] or {}).get("sha") if parents else None) or oldest.get("sha")
                            if isinstance(base_sha, str) and base_sha and base_sha != tip_sha:
                                cmp0 = await client.compare_refs(owner, repo_name, base_sha, tip_sha)
                                if cmp0 is not None and int(cmp0.get("ahead_by") or 0) > 0:
                                    paths0 = collect_paths_from_compare_files(cmp0)
                                    repository0 = {
                                        "id": repository_id,
                                        "full_name": full_name,
                                        "html_url": repo_json.get("html_url"),
                                        "clone_url": repo_json.get("clone_url"),
                                        "ssh_url": repo_json.get("ssh_url"),
                                        "default_branch": repo_json.get("default_branch"),
                                    }
                                    result0 = await apply_github_push(
                                        db,
                                        repository0,
                                        repository_id,
                                        branch,
                                        paths0,
                                        kind="catchup",
                                        max_jobs=jobs_left,
                                    )
                                    if result0.get("jobs"):
                                        made = len(result0["jobs"])
                                        summary["jobs"] += made
                                        jobs_left -= made
                                    summary["first_run_backfill_compares"] += 1
                                    did_backfill = True
                    await upsert_github_push_cursor(db, repository_id, branch, tip_sha)
                    if did_backfill:
                        logger.info(
                            "GitHub catch-up first-run backfill executed for %s branch=%s window=%s tip=%s",
                            full_name,
                            branch,
                            bootstrap_n,
                            tip_sha[:7],
                        )
                    else:
                        logger.info(
                            "GitHub catch-up stored first tip for %s branch=%s at %s. "
                            "No builds queued before this point.",
                            full_name,
                            branch,
                            tip_sha[:7],
                        )
                    continue

                if cur.head_sha == tip_sha:
                    continue

                cmp = await client.compare_refs(owner, repo_name, cur.head_sha, tip_sha)
                if cmp is None:
                    logger.warning(
                        "GitHub compare failed reset cursor repo=%s branch=%s (force-push or missing ref)",
                        full_name,
                        branch,
                    )
                    await upsert_github_push_cursor(db, repository_id, branch, tip_sha)
                    summary["cursor_advanced_after_compare"] += 1
                    continue

                st = cmp.get("status")
                if st == "diverged":
                    logger.warning("GitHub compare diverged reset cursor repo=%s branch=%s", full_name, branch)
                    await upsert_github_push_cursor(db, repository_id, branch, tip_sha)
                    summary["cursor_advanced_after_compare"] += 1
                    continue

                ahead = int(cmp.get("ahead_by") or 0)
                if ahead <= 0:
                    await upsert_github_push_cursor(db, repository_id, branch, tip_sha)
                    summary["cursor_advanced_after_compare"] += 1
                    continue

                if cmp.get("truncated"):
                    logger.warning(
                        "GitHub compare truncated for %s %s… fewer paths than commits",
                        full_name,
                        branch,
                    )

                paths = collect_paths_from_compare_files(cmp)
                repository = {
                    "id": repository_id,
                    "full_name": full_name,
                    "html_url": repo_json.get("html_url"),
                    "clone_url": repo_json.get("clone_url"),
                    "ssh_url": repo_json.get("ssh_url"),
                    "default_branch": repo_json.get("default_branch"),
                }
                result = await apply_github_push(
                    db,
                    repository,
                    repository_id,
                    branch,
                    paths,
                    kind="catchup",
                    max_jobs=jobs_left,
                )
                await upsert_github_push_cursor(db, repository_id, branch, tip_sha)
                summary["cursor_advanced_after_compare"] += 1
                if result.get("jobs"):
                    made = len(result["jobs"])
                    summary["jobs"] += made
                    jobs_left -= made
            except Exception as e:
                logger.exception("GitHub catch-up pair failed repo_id=%s branch=%s", repository_id, branch)
                summary["errors"].append(f"{repository_id}@{branch}: {e}")
                try:
                    await db.rollback()
                except Exception:
                    logger.exception("GitHub catch-up rollback after pair failure failed")

        logger.info("GitHub push catch-up summary %s", summary)
        out: dict[str, Any] = {"status": "ok"}
        out.update(summary)
        return out
    finally:
        await client.close()
