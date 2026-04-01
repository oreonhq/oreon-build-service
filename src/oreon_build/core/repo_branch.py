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

"""R2 repository path segment for published RPMs (not the same as a Git branch name)."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oreon_build.models.release import Release


def default_repo_branch_segment(value: str | None) -> str:
    """Normalize ``releases.default_repo_branch`` (or SQL column) to an R2 path segment."""
    if value is None:
        return "dev"
    s = str(value).strip()
    if not s:
        return "dev"
    return s[:32]


def r2_publish_branch(release: Release | None) -> str:
    """
    Folder name under ``<releasename>/`` where successful builds are published.

    Always comes from :attr:`Release.default_repo_branch`, never from Git or
    ``build_jobs.branch`` alone, so pushes to ``main`` etc. do not create
    ``oreon-11-rp1/main/...`` in R2 by accident.
    """
    if release is None:
        return "dev"
    return default_repo_branch_segment(getattr(release, "default_repo_branch", None))
