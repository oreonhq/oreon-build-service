from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .account import Account
    from .build import BuildJob
    from .release import Release
    from .side_tag import SideTag


class LayerPlanStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LayerPlan(Base):
    """A reactive multi-package build request.

    Unlike the old chain/stage model, this has no precomputed stage batches.
    Root packages are seeded once (static BR resolution only), everything
    buildable now goes straight to `queued`, everything else goes `blocked`
    and is pulled forward reactively as repo publish events land.
    """

    __tablename__ = "layer_plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"), nullable=False, index=True)
    triggered_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    status: Mapped[LayerPlanStatus] = mapped_column(
        Enum(LayerPlanStatus, values_callable=lambda x: [e.value for e in x]),
        default=LayerPlanStatus.PENDING,
        nullable=False,
        index=True,
    )
    root_packages: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    plan_options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    side_tag_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("side_tags.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    release: Mapped["Release"] = relationship("Release")
    triggered_by: Mapped[Optional["Account"]] = relationship("Account")
    side_tag: Mapped[Optional["SideTag"]] = relationship("SideTag", back_populates="layer_plans")
    jobs: Mapped[List["BuildJob"]] = relationship("BuildJob", back_populates="layer_plan")


class LayerJobBlocker(Base):
    """Reactive-wakeup index: which distgit source a blocked job is waiting on.

    A createrepo_c publish event for `blocking_source` looks these rows up
    directly (indexed) instead of re-checking every blocked job in the plan -
    this is what keeps unblocking O(published packages) instead of
    O(all blocked jobs) per event.
    """

    __tablename__ = "layer_job_blockers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    build_job_id: Mapped[int] = mapped_column(ForeignKey("build_jobs.id"), nullable=False, index=True)
    blocking_source: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_layer_job_blockers_source_job", "blocking_source", "build_job_id"),
    )
