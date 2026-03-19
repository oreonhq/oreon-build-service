from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .account import Account


class Promotion(Base):
    __tablename__ = "promotions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(nullable=False)
    from_channel: Mapped[str] = mapped_column(String(32), nullable=False)
    to_channel: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # null when status=pending
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)  # pending, promoted_testing, promoted_stable, kept_dev
    package_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    package_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    build_job_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    promoted_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    promoted_by: Mapped[Optional["Account"]] = relationship("Account")
