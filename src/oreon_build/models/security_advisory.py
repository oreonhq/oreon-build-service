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

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .account import Account
    from .release import Release


class SecurityAdvisory(Base):
    """
    Custom (admin-created) security advisories that are not necessarily discovered via OSV.
    """

    __tablename__ = "security_advisories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False, index=True)

    # Optional CVE identifier for user-provided context.
    cve_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    package_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, index=True)
    package_version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    upstream_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recommended_fix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    is_handled: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false", index=True)
    handled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    handled_by_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True
    )

    releases: Mapped[list["SecurityAdvisoryRelease"]] = relationship(
        "SecurityAdvisoryRelease", back_populates="advisory", cascade="all, delete-orphan"
    )


class SecurityAdvisoryRelease(Base):
    """
    Many-to-many mapping: which Oreon releases are affected by this custom advisory.
    """

    __tablename__ = "security_advisory_releases"

    advisory_id: Mapped[int] = mapped_column(
        ForeignKey("security_advisories.id"), primary_key=True
    )
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    advisory: Mapped["SecurityAdvisory"] = relationship("SecurityAdvisory", back_populates="releases")
    release: Mapped["Release"] = relationship("Release")

