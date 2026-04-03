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

"""GitHub push webhook and DistGit helper routes."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, List

from fastapi import APIRouter, HTTPException, Request, status

from oreon_build.api.deps import DbSession
from oreon_build.config import get_settings
from oreon_build.services.github import get_github_client
from oreon_build.services.github_push_catchup import upsert_github_push_cursor
from oreon_build.services.github_push_trigger import (
    apply_github_push,
    resolve_github_webhook_changed_paths,
)

router = APIRouter(prefix="/github", tags=["github"])
logger = logging.getLogger(__name__)


def _push_ref_branch(body: dict[str, Any]) -> str | None:
    ref = body.get("ref") or ""
    if isinstance(ref, str) and ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :] or None
    return None


def _verify_github_signature(body_bytes: bytes, signature_header: str | None, secret: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    mac = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    expected = "sha256=" + mac
    return hmac.compare_digest(signature_header, expected)


@router.post("/webhook")
async def github_push_webhook(request: Request, db: DbSession) -> dict[str, Any]:
    """Handle GitHub push events. Match packages by repository id or github_html_url."""
    settings = get_settings()
    body_bytes = await request.body()

    if settings.github_webhook_secret:
        sig = request.headers.get("X-Hub-Signature-256")
        if not _verify_github_signature(body_bytes, sig, settings.github_webhook_secret):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

    event = request.headers.get("X-GitHub-Event") or ""
    if event == "ping":
        logger.info("GitHub webhook ping ok")
        return {"status": "ok", "reason": "ping"}
    if event != "push":
        logger.info("GitHub webhook ignored event=%s", event)
        return {"status": "ignored", "reason": "unsupported_event"}

    repository = body.get("repository") or {}
    repo_id = repository.get("id")
    if not repo_id:
        logger.warning("GitHub webhook ignored no_repository_id in payload")
        return {"status": "ignored", "reason": "no_repository_id"}

    if body.get("deleted"):
        logger.info("GitHub webhook ignored deleted ref")
        return {"status": "ignored", "reason": "deleted_ref"}

    repo_id = int(repo_id)
    push_branch = _push_ref_branch(body)
    changed_paths = await resolve_github_webhook_changed_paths(body, repository)
    result = await apply_github_push(db, repository, repo_id, push_branch, changed_paths, kind="webhook")

    after_sha = body.get("after")
    branch_for_cursor = push_branch or (repository.get("default_branch") or "").strip()
    if after_sha and isinstance(after_sha, str) and branch_for_cursor:
        await upsert_github_push_cursor(db, repo_id, branch_for_cursor, after_sha)

    fn = (repository.get("full_name") or "").strip()
    logger.info(
        "GitHub webhook done repo=%s repo_id=%s branch=%s result=%s",
        fn or "?",
        repo_id,
        branch_for_cursor or push_branch,
        result.get("status"),
    )
    return result


@router.get("/repos", response_model=List[dict])
async def list_org_repositories(search: str | None = None) -> List[dict[str, Any]]:
    """List GitHub repos for the configured org (DistGit UI)."""
    settings = get_settings()
    if not settings.github_org:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GITHUB_ORG is not configured",
        )
    client = get_github_client()
    try:
        raw = await client.list_org_repositories(settings.github_org)
    finally:
        await client.close()
    out: List[dict[str, Any]] = []
    q = (search or "").lower()
    for r in raw:
        name = r.get("name") or ""
        full_name = r.get("full_name") or ""
        if q and q not in name.lower() and q not in full_name.lower():
            continue
        out.append(
            {
                "id": r.get("id"),
                "name": name,
                "path": full_name,
                "web_url": r.get("html_url"),
                "default_branch": r.get("default_branch"),
            }
        )
    return out


@router.get("/repos/{repository_id}/branches", response_model=List[dict])
async def list_repository_branches_api(repository_id: int) -> List[dict[str, Any]]:
    """List branches for a GitHub repository (numeric id)."""
    client = get_github_client()
    repo: dict[str, Any] | None = None
    branches: list[dict[str, Any]] = []
    try:
        repo = await client.get_repository_by_id(repository_id)
        if not repo:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")
        full_name = (repo.get("full_name") or "").strip()
        if "/" not in full_name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Repository missing full_name")
        owner, repo_name = full_name.split("/", 1)
        branches = await client.list_repository_branches(owner, repo_name)
    finally:
        await client.close()
    default_branch = ((repo or {}).get("default_branch") or "").strip()
    out: List[dict[str, Any]] = []
    for b in branches:
        name = (b.get("name") or "").strip()
        out.append({"name": name, "default": bool(name and name == default_branch)})
    return out
