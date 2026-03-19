from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from oreon_build.db.base import Base


class SigningJobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class SigningJob(Base):
    __tablename__ = "signing_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    repository_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    release_id: Mapped[int] = mapped_column(nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    architecture: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[SigningJobStatus] = mapped_column(
        Enum(SigningJobStatus, values_callable=lambda x: [e.value for e in x]),
        default=SigningJobStatus.PENDING,
        nullable=False,
    )
    r2_prefix: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
