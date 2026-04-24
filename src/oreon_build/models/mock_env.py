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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, func
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
    # Extra mock --addrepo base URLs (http(s)), from web UI; worker receives them on poll.
    additional_repo_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # Secondary lookaside (e.g. Fedora) for bootstrap; merged after additional_repo_urls for --addrepo.
    lookaside_repo_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    release: Mapped["Release"] = relationship("Release", back_populates="mock_environments")
