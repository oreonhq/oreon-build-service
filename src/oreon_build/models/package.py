from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .build import BuildJob
    from .mock_env import MockEnvironment
    from .release import Release


class Package(Base):
    __tablename__ = "packages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gitlab_project_id: Mapped[Optional[int]] = mapped_column(nullable=True, index=True)
    gitlab_web_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    preferred_mock_environment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("mock_environments.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    versions: Mapped[List["PackageVersion"]] = relationship(
        "PackageVersion", back_populates="package", order_by="PackageVersion.created_at.desc()"
    )
    build_jobs: Mapped[List["BuildJob"]] = relationship("BuildJob", back_populates="package")
    preferred_mock_environment: Mapped[Optional["MockEnvironment"]] = relationship(
        "MockEnvironment", foreign_keys=[preferred_mock_environment_id]
    )


class PackageVersion(Base):
    __tablename__ = "package_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id"), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    release_tag: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    git_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    package: Mapped["Package"] = relationship("Package", back_populates="versions")
    sources: Mapped[List["Source"]] = relationship("Source", back_populates="package_version")


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    package_version_id: Mapped[int] = mapped_column(ForeignKey("package_versions.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    r2_key: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    package_version: Mapped["PackageVersion"] = relationship(
        "PackageVersion", back_populates="sources"
    )
