from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .release import Release


class MockEnvironment(Base):
    __tablename__ = "mock_environments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    config_name: Mapped[str] = mapped_column(String(128), nullable=False)  # base name; worker uses config_name + "-" + arch
    architecture: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # deprecated; kept for backward compat
    config_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    release: Mapped["Release"] = relationship("Release", back_populates="mock_environments")
