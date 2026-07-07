from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oreon_build.models.release import Release

def default_repo_branch_segment(value: str | None) -> str:
    if value is None:
        return "dev"
    s = str(value).strip()
    if not s:
        return "dev"
    return s[:32]

def r2_publish_branch(release: Release | None) -> str:
    if release is None:
        return "dev"
    return default_repo_branch_segment(getattr(release, "default_repo_branch", None))
