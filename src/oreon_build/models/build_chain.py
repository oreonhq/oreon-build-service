from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .account import Account
    from .build import BuildJob
    from .release import Release


class BuildChainStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BuildChain(Base):
    """Chain build: stages of parallel distgit jobs, gated on repo publish."""

    __tablename__ = "build_chains"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"), nullable=False, index=True)
    triggered_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    status: Mapped[BuildChainStatus] = mapped_column(
        Enum(BuildChainStatus, values_callable=lambda x: [e.value for e in x]),
        default=BuildChainStatus.PENDING,
        nullable=False,
        index=True,
    )
    chain_spec: Mapped[str] = mapped_column(Text, nullable=False)
    #: Per-stage enqueue settings (arches, skip_complete, distgit, mock, priority).
    chain_options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    stages_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    active_stage_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Jobs created for the active stage (each needs a successful repo publish before the chain advances).
    stage_expected_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Slots satisfied by skip_complete (already successful for that package or arch).
    stage_skip_slots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stage_publish_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: When set, the chain waits until this timestamp before advancing to the next stage.
    #: Allows a settle period after all stage publishes finish so workers see fresh repodata.
    stage_advance_after: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    release: Mapped["Release"] = relationship("Release")
    triggered_by: Mapped[Optional["Account"]] = relationship("Account")
    jobs: Mapped[List["BuildJob"]] = relationship("BuildJob", back_populates="build_chain")


class BuildChainPublishReceipt(Base):
    """Idempotent marker so repo publish retries do not double-count chain stage progress."""

    __tablename__ = "build_chain_publish_receipts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    build_chain_id: Mapped[int] = mapped_column(ForeignKey("build_chains.id"), nullable=False, index=True)
    build_attempt_id: Mapped[int] = mapped_column(ForeignKey("build_attempts.id"), nullable=False, unique=True)

    build_chain: Mapped["BuildChain"] = relationship("BuildChain")
