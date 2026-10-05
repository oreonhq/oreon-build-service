from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from oreon_build.db.base import Base
from oreon_build.models.build import BuildStatus


class IsoBuild(Base):
    __tablename__ = "iso_builds"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(32), default=BuildStatus.PENDING.value, nullable=False, index=True)
    architecture: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    mock_environment_id: Mapped[int] = mapped_column(ForeignKey("mock_environments.id"), nullable=False)
    mock_config: Mapped[str] = mapped_column(String(128), nullable=False)
    release_folder: Mapped[str] = mapped_column(String(128), nullable=False)
    kickstart: Mapped[str] = mapped_column(String(256), nullable=False)
    iso_name: Mapped[str] = mapped_column(String(256), nullable=False)
    project: Mapped[str] = mapped_column(String(64), nullable=False, default="Oreon")
    volid: Mapped[str] = mapped_column(String(32), nullable=False)
    releasever: Mapped[str] = mapped_column(String(16), nullable=False)
    macboot: Mapped[bool] = mapped_column(nullable=False, default=False)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    r2_key: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    log_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    worker_id: Mapped[Optional[int]] = mapped_column(ForeignKey("workers.id"), nullable=True)
    triggered_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
