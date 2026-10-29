# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .build import BuildJob
    from .release import Release


class RpmbuildPlanStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_SETTLE = "waiting_settle"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RpmbuildPlan(Base):
    __tablename__ = "rpmbuild_plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"), nullable=False, index=True)
    releasename: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    repo_branch: Mapped[str] = mapped_column(String(64), nullable=False, default="dev")
    distgit_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    distgit_branch: Mapped[str] = mapped_column(String(128), nullable=False)
    seed_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="minimal_seed")
    seed_packages_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    layers_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    options_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[RpmbuildPlanStatus] = mapped_column(
        Enum(RpmbuildPlanStatus, values_callable=lambda x: [e.value for e in x]),
        default=RpmbuildPlanStatus.PENDING,
        nullable=False,
        index=True,
    )
    current_open_layer: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settle_deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    release: Mapped["Release"] = relationship("Release")
    jobs: Mapped[List["BuildJob"]] = relationship("BuildJob", back_populates="rpmbuild_plan")
