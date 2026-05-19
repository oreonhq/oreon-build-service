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

"""GitHub REST API client for org repo lists and branches (DistGit UI)."""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

import httpx

from oreon_build.config import get_settings

logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self) -> None:
        s = get_settings()
        self.base_url = (s.github_api_url or "https://api.github.com").rstrip("/")
        self.token = s.github_token
        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=60.0,
        )

    async def get_repository_by_id(self, repo_id: int) -> Optional[dict[str, Any]]:
        r = await self._client.get(f"/repositories/{repo_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def list_org_repositories(self, org: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        org_enc = org.strip().strip("/")
        while True:
            r = await self._client.get(
                f"/orgs/{org_enc}/repos",
                params={"per_page": 100, "page": page, "type": "all"},
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    async def list_repository_branches(self, owner: str, repo: str) -> list[dict[str, Any]]:
        r = await self._client.get(
            f"/repos/{owner}/{repo}/branches",
            params={"per_page": 100},
        )
        r.raise_for_status()
        return r.json()

    async def get_repository(self, owner: str, repo: str) -> Optional[dict[str, Any]]:
        r = await self._client.get(f"/repos/{owner}/{repo}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def compare_refs(self, owner: str, repo: str, base: str, head: str) -> Optional[dict[str, Any]]:
        """GET repos/compare/{base}...{head} . Returns None on 404/422 (unrelated/missing)."""
        spec = f"{quote(base, safe='')}...{quote(head, safe='')}"
        r = await self._client.get(f"/repos/{owner}/{repo}/compare/{spec}")
        if r.status_code in (404, 422):
            return None
        r.raise_for_status()
        return r.json()

    async def list_commits_on_ref(self, owner: str, repo: str, ref: str, per_page: int = 1) -> list[dict[str, Any]]:
        r = await self._client.get(
            f"/repos/{owner}/{repo}/commits",
            params={"sha": ref, "per_page": per_page},
        )
        r.raise_for_status()
        return r.json()

    async def list_git_tree_recursive(self, owner: str, repo: str, ref: str) -> dict[str, Any]:
        """Return the full recursive git tree for a ref.

        The response has ``tree`` (list of entries with ``path``, ``type``, ``sha``) and
        ``truncated`` (bool). Callers must handle truncated=True (repo is too large).
        """
        r = await self._client.get(
            f"/repos/{owner}/{repo}/git/trees/{quote(ref, safe='')}",
            params={"recursive": "1"},
        )
        r.raise_for_status()
        return r.json()

    async def get_repo_contents(self, owner: str, repo: str, path: str, ref: str | None = None) -> Any:
        """Return the GitHub Contents API response for a path (file or directory listing)."""
        params = {}
        if ref:
            params["ref"] = ref
        r = await self._client.get(
            f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
            params=params,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def close(self) -> None:
        await self._client.aclose()


def get_github_client() -> GitHubClient:
    return GitHubClient()
