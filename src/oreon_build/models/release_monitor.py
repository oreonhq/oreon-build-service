from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base


class ReleaseMonitorScan(Base):

    __tablename__ = "release_monitor_scans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    release_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("releases.id"), nullable=True, index=True
    )
    branches: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    side_tag_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("side_tags.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", index=True)
    total_packages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checked_packages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_packages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outdated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id"), nullable=True
    )

    outdated: Mapped[list["ReleaseMonitorOutdated"]] = relationship(
        "ReleaseMonitorOutdated", back_populates="scan", cascade="all, delete-orphan"
    )


class ReleaseMonitorOutdated(Base):

    __tablename__ = "release_monitor_outdated"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(
        ForeignKey("release_monitor_scans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("releases.id"), nullable=True, index=True
    )
    package_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    current_version: Mapped[str] = mapped_column(String(256), nullable=False)
    latest_version: Mapped[str] = mapped_column(String(256), nullable=False)
    project: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    ecosystem: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    scan: Mapped["ReleaseMonitorScan"] = relationship(
        "ReleaseMonitorScan", back_populates="outdated"
    )
