from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .package import Package, PackageVersion
    from .release import Release


class BuildStatus(str, enum.Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class BuildTarget(Base):
    __tablename__ = "build_targets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"), nullable=False)
    architecture: Mapped[str] = mapped_column(String(32), nullable=False)
    mock_environment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("mock_environments.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    release: Mapped["Release"] = relationship("Release")
    mock_environment: Mapped[Optional["MockEnvironment"]] = relationship(
        "MockEnvironment", foreign_keys=[mock_environment_id]
    )
    build_jobs: Mapped[List["BuildJob"]] = relationship(
        "BuildJob", back_populates="build_target", foreign_keys="BuildJob.target_id"
    )


class BuildJob(Base):
    __tablename__ = "build_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id"), nullable=False)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"), nullable=False)
    target_id: Mapped[Optional[int]] = mapped_column(ForeignKey("build_targets.id"), nullable=True)
    package_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("package_versions.id"), nullable=True
    )
    status: Mapped[BuildStatus] = mapped_column(
        Enum(BuildStatus, values_callable=lambda x: [e.value for e in x]),
        default=BuildStatus.PENDING,
        nullable=False,
        index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    triggered_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    branch: Mapped[str] = mapped_column(String(32), default="dev", nullable=False)

    package: Mapped["Package"] = relationship("Package", back_populates="build_jobs")
    release: Mapped["Release"] = relationship("Release", back_populates="build_jobs")
    package_version: Mapped[Optional["PackageVersion"]] = relationship(
        "PackageVersion", foreign_keys=[package_version_id]
    )
    build_target: Mapped[Optional["BuildTarget"]] = relationship(
        "BuildTarget", back_populates="build_jobs", foreign_keys=[target_id]
    )
    attempts: Mapped[List["BuildAttempt"]] = relationship(
        "BuildAttempt", back_populates="build_job", order_by="BuildAttempt.started_at.desc()"
    )

    @property
    def architecture(self) -> Optional[str]:
        """Architecture from build_target (for API response)."""
        if self.build_target is not None:
            return self.build_target.architecture
        return None


class BuildAttempt(Base):
    __tablename__ = "build_attempts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    build_job_id: Mapped[int] = mapped_column(ForeignKey("build_jobs.id"), nullable=False)
    worker_id: Mapped[Optional[int]] = mapped_column(ForeignKey("workers.id"), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[BuildStatus] = mapped_column(
        Enum(BuildStatus, values_callable=lambda x: [e.value for e in x]),
        default=BuildStatus.PENDING,
        nullable=False,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    log_r2_key: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    build_job: Mapped["BuildJob"] = relationship("BuildJob", back_populates="attempts")
    worker: Mapped[Optional["Worker"]] = relationship(
        "Worker", back_populates="attempts", foreign_keys="BuildAttempt.worker_id"
    )
    artifacts: Mapped[List["Artifact"]] = relationship(
        "Artifact", back_populates="build_attempt", cascade="all, delete-orphan"
    )
