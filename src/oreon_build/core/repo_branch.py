# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oreon_build.models.release import Release

PRIMARY_REPO_BRANCHES = ("dev", "testing", "stable")


def default_repo_branch_segment(value: str | None) -> str:
    if value is None:
        return "dev"
    s = str(value).strip()
    if not s:
        return "dev"
    return s[:128]


def r2_publish_branch(release: Release | None) -> str:
    """Release default primary branch (dev/testing/stable). Not for new side-tag builds."""
    if release is None:
        return "dev"
    return default_repo_branch_segment(getattr(release, "default_repo_branch", None))


def job_publish_branch(job_branch: str | None, release: Release | None = None) -> str:
    """Publish target for a job. Prefer job.branch (side tag); fall back to release default."""
    s = (job_branch or "").strip()
    if s:
        return s[:128]
    return r2_publish_branch(release)


def release_distgit_branch(release: Release | None) -> str:
    if release is None:
        return "main"
    raw = getattr(release, "default_distgit_branch", None)
    s = (str(raw).strip() if raw else "") or "main"
    return s[:128]
