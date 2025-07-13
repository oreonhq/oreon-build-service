"""GitLab integration: fetch sources, trigger builds from webhooks."""
from __future__ import annotations

import logging
import tarfile
import io
from typing import Any, Optional

import httpx

from oreon_build.config import get_settings

logger = logging.getLogger(__name__)


class GitLabClient:
    def __init__(self) -> None:
        s = get_settings()
        self.base_url = s.gitlab_url.rstrip("/")
        self.token = s.gitlab_private_token
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v4",
            headers={"PRIVATE-TOKEN": self.token} if self.token else {},
            timeout=60.0,
        )

    async def get_project(self, project_id: int) -> Optional[dict[str, Any]]:
        resp = await self._client.get(f"/projects/{project_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def get_file(self, project_id: int, ref: str, file_path: str) -> Optional[bytes]:
        path = f"/projects/{project_id}/repository/files/{file_path.replace('/', '%2F')}/raw"
        resp = await self._client.get(path, params={"ref": ref})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    async def get_archive(self, project_id: int, ref: str = "HEAD", format: str = "tar.gz") -> Optional[bytes]:
        resp = await self._client.get(f"/projects/{project_id}/repository/archive", params={"sha": ref})
        if resp.status_code != 200:
            return None
        return resp.content

    async def list_group_projects(self, group_id: str) -> list[dict[str, Any]]:
        resp = await self._client.get(f"/groups/{group_id}/projects", params={"per_page": 100})
        resp.raise_for_status()
        return resp.json()

    async def list_project_branches(self, project_id: int) -> list[dict[str, Any]]:
        resp = await self._client.get(f"/projects/{project_id}/repository/branches", params={"per_page": 100})
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()


def get_gitlab_client() -> GitLabClient:
    return GitLabClient()
