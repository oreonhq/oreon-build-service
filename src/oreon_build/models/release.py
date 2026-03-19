from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .build import BuildJob
    from .mock_env import MockEnvironment
    from .repo import Repository
    from .schedule import Schedule


class ReleaseChannel(str, enum.Enum):
    DEV = "dev"
    TESTING = "testing"
    STABLE = "stable"


class Release(Base):
    __tablename__ = "releases"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    releasename: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    architectures: Mapped[str] = mapped_column(String(256), nullable=False)
    default_channel: Mapped[ReleaseChannel] = mapped_column(
        Enum(ReleaseChannel, values_callable=lambda x: [e.value for e in x]),
        default=ReleaseChannel.DEV,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    base_repos: Mapped[List["ReleaseRepo"]] = relationship(
        "ReleaseRepo", back_populates="release", cascade="all, delete-orphan"
    )
    build_jobs: Mapped[List["BuildJob"]] = relationship("BuildJob", back_populates="release")
    mock_environments: Mapped[List["MockEnvironment"]] = relationship(
        "MockEnvironment", back_populates="release"
    )
    repositories: Mapped[List["Repository"]] = relationship(
        "Repository", back_populates="release", cascade="all, delete-orphan"
    )
    schedules: Mapped[List["Schedule"]] = relationship(
        "Schedule", back_populates="release", cascade="all, delete-orphan"
    )

    def arch_list(self) -> list[str]:
        return [a.strip() for a in self.architectures.split(",") if a.strip()]


class ReleaseRepo(Base):
    """Base dependency repo URL for a release (used by mock)."""

    __tablename__ = "release_repos"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    baseurl: Mapped[str] = mapped_column(String(1024), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    release: Mapped["Release"] = relationship("Release", back_populates="base_repos")
