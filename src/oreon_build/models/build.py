from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .layer_plan import LayerPlan
    from .package import Package, PackageVersion
    from .release import Release
    from .rpmbuild_plan import RpmbuildPlan
    from .side_tag import SideTag


class BuildStatus(str, enum.Enum):
    PENDING = "pending"
    QUEUED = "queued"
    BLOCKED = "blocked"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CANCELED = "cancelled"
    SKIPPED = "skipped"


class BuildPhase(str, enum.Enum):
    """The toolchain provenance of a build job."""

    PHASE1_BRIDGE = "phase1_bridge"
    PHASE2_NATIVE = "phase2_native"


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
    branch: Mapped[str] = mapped_column(String(128), default="dev", nullable=False)
    side_tag_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("side_tags.id"), nullable=True, index=True
    )
    layer_plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("layer_plans.id"), nullable=True, index=True)
    layer_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    build_phase: Mapped[str] = mapped_column(
        String(32), default=BuildPhase.PHASE1_BRIDGE.value,
        server_default=BuildPhase.PHASE1_BRIDGE.value, nullable=False, index=True,
    )
    target_mock_config: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    blocking_sources: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    rpmbuild_plan_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("rpmbuild_plans.id"), nullable=True, index=True
    )
    rpmbuild_layer_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    rpmbuild_wave_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    job_options: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

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
    layer_plan: Mapped[Optional["LayerPlan"]] = relationship("LayerPlan", back_populates="jobs")
    side_tag: Mapped[Optional["SideTag"]] = relationship("SideTag", back_populates="jobs")
    rpmbuild_plan: Mapped[Optional["RpmbuildPlan"]] = relationship("RpmbuildPlan", back_populates="jobs")

    @property
    def architecture(self) -> Optional[str]:
        if self.build_target is not None:
            return self.build_target.architecture
        return None


class BuildAttempt(Base):
    __tablename__ = "build_attempts"
    __table_args__ = (
        Index(
            "ux_build_attempts_one_running_per_job",
            "build_job_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
    )

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
    mock_succeeded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    signing_status: Mapped[str] = mapped_column(String(32), default="not_started", nullable=False)
    build_profile: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    rpmbuild_defines: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    failure_kind: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    failure_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    failure_details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    build_job: Mapped["BuildJob"] = relationship("BuildJob", back_populates="attempts")
    worker: Mapped[Optional["Worker"]] = relationship(
        "Worker", back_populates="attempts", foreign_keys="BuildAttempt.worker_id"
    )
    artifacts: Mapped[List["Artifact"]] = relationship(
        "Artifact", back_populates="build_attempt", cascade="all, delete-orphan"
    )
