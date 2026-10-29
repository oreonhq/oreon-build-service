# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from oreon_build.db.base import Base

class RpmCompleteLock(Base):

    __tablename__ = "rpm_complete_locks"

    attempt_id: Mapped[int] = mapped_column(primary_key=True)
    r2_key: Mapped[str] = mapped_column(primary_key=True)

    owner: Mapped[str] = mapped_column(String(128), nullable=False, default=lambda: str(uuid4()))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

