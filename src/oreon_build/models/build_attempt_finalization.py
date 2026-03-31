#
# Oreon Build Service
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .build import BuildAttempt


class BuildAttemptFinalization(Base):
    """
    Idempotency guard for `POST /api/worker/result/{attempt_id}`.

    Once a build attempt reaches a terminal state (success/failed/cancelled),
    the controller stores one row keyed by `attempt_id`.
    """

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

    # Relationship intentionally omitted to keep the model minimal.

