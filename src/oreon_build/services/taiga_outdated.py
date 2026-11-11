# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import delete, select

from oreon_build.config import get_settings
from oreon_build.db.session import async_session_maker
from oreon_build.models import Release, ReleaseMonitorTaigaIssue

logger = logging.getLogger(__name__)


def issue_subject(package: str) -> str:
    return package + " is outdated"


def issue_body(package: str, current: str, latest: str) -> str:
    return (
        package + " is outdated. we still have " + current + ".\n"
        "\n"
        "latest upstream\n"
        + latest
        + "\n"
    )


def parse_groups(text: str) -> dict[str, list[str]]:
    maintainers: list[str] = []
    packages: list[str] = []
    out: dict[str, list[str]] = {}

    def flush() -> None:
        if not packages:
            return
        names = [m.strip() for m in maintainers if m.strip()]
        for pkg in packages:
            out[pkg] = list(names)

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            flush()
            maintainers = []
            packages = []
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "maintainer":
            maintainers = [part.strip() for part in value.split(",") if part.strip()]
        elif key == "packages":
            packages = [part.strip() for part in value.split(",") if part.strip()]
    flush()
    return out


def match_assignees(maintainers: list[str], users: list[dict[str, Any]]) -> list[int]:
    want = {m.casefold() for m in maintainers if m and m.strip()}
    if not want:
        return []
    ids: list[int] = []
    seen: set[int] = set()
    for user in users:
        names = set()
        username = str(user.get("username") or "").strip()
        full_name = str(user.get("full_name") or "").strip()
        if username:
            names.add(username.casefold())
        if full_name:
            names.add(full_name.casefold())
        uid = user.get("id")
        if not names or uid is None or int(uid) in seen:
            continue
        if names & want:
            seen.add(int(uid))
            ids.append(int(uid))
    return ids


class TaigaClient:
    def __init__(self, client: httpx.AsyncClient, base: str, headers: dict[str, str]):
        self.client = client
        self.base = base.rstrip("/")
        self.headers = headers

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = await self.client.get(self.base + path, params=params, headers=self.headers)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        r = await self.client.post(self.base + path, json=payload, headers=self.headers)
        r.raise_for_status()
        return r.json()

    async def project_id(self, slug: str) -> int:
        data = await self._get("/projects/by_slug", {"slug": slug})
        return int(data["id"])

    async def project_users(self, project_id: int) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        page = 1
        while page < 50:
            data = await self._get("/users", {"project": project_id, "page": page})
            if isinstance(data, dict):
                batch = data.get("results") or data.get("users") or []
            else:
                batch = data
            if not batch:
                break
            users.extend(batch)
            if len(batch) < 30:
                break
            page += 1
        return users

    async def create_issue(
        self,
        project_id: int,
        subject: str,
        description: str,
        assignee_ids: list[int],
    ) -> int:
        payload: dict[str, Any] = {
            "project": project_id,
            "subject": subject,
            "description": description,
        }
        if assignee_ids:
            payload["assigned_to"] = assignee_ids[0]
            payload["watchers"] = assignee_ids
        data = await self._post("/issues", payload)
        return int(data["id"])

    async def delete_issue(self, issue_id: int) -> None:
        r = await self.client.delete(self.base + "/issues/" + str(issue_id), headers=self.headers)
        if r.status_code == 404:
            return
        r.raise_for_status()


async def _auth_headers(client: httpx.AsyncClient, base: str) -> dict[str, str] | None:
    settings = get_settings()
    token = (settings.taiga_token or "").strip()
    if token:
        return {"Authorization": "Application " + token, "Content-Type": "application/json"}
    username = (settings.taiga_username or "").strip()
    password = settings.taiga_password.get_secret_value().strip() if settings.taiga_password else ""
    if not username or not password:
        return None
    r = await client.post(
        base.rstrip("/") + "/auth",
        json={"type": "normal", "username": username, "password": password},
    )
    r.raise_for_status()
    auth = str(r.json().get("auth_token") or "").strip()
    if not auth:
        return None
    return {"Authorization": "Bearer " + auth, "Content-Type": "application/json"}


def _configured() -> bool:
    settings = get_settings()
    if not (settings.taiga_api_url or "").strip() or not (settings.taiga_project_slug or "").strip():
        return False
    if (settings.taiga_token or "").strip():
        return True
    if (settings.taiga_username or "").strip() and settings.taiga_password:
        return True
    return False


async def file_outdated_taiga_issues(release_id: int | None, rows: list[dict[str, Any]]) -> None:
    if not _configured():
        return
    settings = get_settings()
    base = settings.taiga_api_url.strip()
    try:
        async with async_session_maker() as db:
            release = await db.get(Release, release_id) if release_id is not None else None
            releasename = release.releasename if release is not None else ""
        if not releasename:
            logger.warning("taiga skip, scan has no releasename")
            return
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            headers = await _auth_headers(client, base)
            if not headers:
                return
            api = TaigaClient(client, base, headers)
            project_id = await api.project_id(settings.taiga_project_slug.strip())
            users = await api.project_users(project_id)
            groups_url = settings.taiga_groups_url.format(release=releasename)
            groups: dict[str, list[str]] = {}
            try:
                got = await client.get(groups_url)
                if got.status_code == 200:
                    groups = parse_groups(got.text)
                else:
                    logger.warning("taiga groups fetch %s -> %s", groups_url, got.status_code)
            except Exception:
                logger.exception("taiga groups fetch failed %s", groups_url)
            current = {(str(row["package_name"]), str(row["latest_version"])) for row in rows}
            async with async_session_maker() as db:
                stored = list(
                    (
                        await db.execute(
                            select(ReleaseMonitorTaigaIssue).where(
                                ReleaseMonitorTaigaIssue.releasename == releasename
                            )
                        )
                    ).scalars().all()
                )
                gone = [
                    (row.id, row.taiga_issue_id, row.package_name, row.latest_version)
                    for row in stored
                    if (row.package_name, row.latest_version) not in current
                ]
            for row_id, issue_id, package, latest in gone:
                if issue_id is not None:
                    try:
                        await api.delete_issue(int(issue_id))
                    except Exception:
                        logger.exception(
                            "taiga delete failed package=%s version=%s issue=%s",
                            package,
                            latest,
                            issue_id,
                        )
                        continue
                async with async_session_maker() as db:
                    await db.execute(
                        delete(ReleaseMonitorTaigaIssue).where(ReleaseMonitorTaigaIssue.id == row_id)
                    )
                    await db.commit()
            for row in rows:
                package = str(row["package_name"])
                latest = str(row["latest_version"])
                current = str(row["current_version"])
                async with async_session_maker() as db:
                    existing = await db.scalar(
                        select(ReleaseMonitorTaigaIssue.id).where(
                            ReleaseMonitorTaigaIssue.releasename == releasename,
                            ReleaseMonitorTaigaIssue.package_name == package,
                            ReleaseMonitorTaigaIssue.latest_version == latest,
                        )
                    )
                if existing is not None:
                    continue
                assignees = match_assignees(groups.get(package, []), users)
                try:
                    issue_id = await api.create_issue(
                        project_id,
                        issue_subject(package),
                        issue_body(package, current, latest),
                        assignees,
                    )
                except Exception:
                    logger.exception("taiga issue failed package=%s", package)
                    continue
                async with async_session_maker() as db:
                    db.add(
                        ReleaseMonitorTaigaIssue(
                            releasename=releasename,
                            package_name=package,
                            latest_version=latest,
                            taiga_issue_id=issue_id,
                        )
                    )
                    await db.commit()
    except Exception:
        logger.exception("taiga outdated filing failed")
