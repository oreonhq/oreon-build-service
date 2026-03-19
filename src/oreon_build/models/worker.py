from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, Enum, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .build import BuildAttempt


class WorkerState(str, enum.Enum):
    IDLE = "idle"
    BUSY = "busy"
    UNHEALTHY = "unhealthy"
    OFFLINE = "offline"
    DRAINING = "draining"


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[WorkerState] = mapped_column(
        Enum(WorkerState, values_callable=lambda x: [e.value for e in x]),
        default=WorkerState.OFFLINE,
        nullable=False,
        index=True,
    )
    architecture: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    current_build_attempt_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    capabilities: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    attempts: Mapped[List["BuildAttempt"]] = relationship(
        "BuildAttempt", back_populates="worker"
    )
