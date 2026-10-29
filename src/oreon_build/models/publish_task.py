# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from oreon_build.db.base import Base

class PublishTask(Base):

    __tablename__ = "publish_tasks"

    __table_args__ = (
        sa.UniqueConstraint("attempt_id", name="ux_publish_tasks_attempt_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="repo_publish")

    attempt_id: Mapped[int] = mapped_column(ForeignKey("build_attempts.id"), nullable=False)
    build_job_id: Mapped[int] = mapped_column(ForeignKey("build_jobs.id"), nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

