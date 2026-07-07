from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .build import BuildAttempt

class BuildAttemptFinalization(Base):

    __tablename__ = "build_attempt_finalizations"

    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("build_attempts.id"), primary_key=True, nullable=False
    )
    finalized_status: Mapped[str] = mapped_column(String(32), nullable=False)
    log_r2_key: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    finalized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

