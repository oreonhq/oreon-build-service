#
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from oreon_build.db.base import Base


class PublishTask(Base):
    """Durable queue entry for expensive R2/repodata publish work after success."""

    __tablename__ = "publish_tasks"

    # One publish per attempt_id is enough for our usage pattern.
    __table_args__ = (
        sa.UniqueConstraint("attempt_id", name="ux_publish_tasks_attempt_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="repo_publish")

    attempt_id: Mapped[int] = mapped_column(ForeignKey("build_attempts.id"), nullable=False)
    build_job_id: Mapped[int] = mapped_column(ForeignKey("build_jobs.id"), nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )  # pending, running, success, failed
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

