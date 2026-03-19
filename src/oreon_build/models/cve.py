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

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .build import BuildJob
    from .release import Release


class TrackedRpm(Base):
    """
    A specific RPM identity we have in the R2 repo: package name + version + release.
    Used as the unit for OSV queries.
    """

    __tablename__ = "tracked_rpms"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    package_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    rpm_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rpm_release: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    matches: Mapped[list["CveMatch"]] = relationship("CveMatch", back_populates="tracked_rpm")

    __table_args__ = (UniqueConstraint("package_name", "rpm_version", "rpm_release", name="uq_tracked_rpm"),)

    @property
    def version_string(self) -> str:
        return f"{self.rpm_version}-{self.rpm_release}"


class CveMatch(Base):
    """
    A CVE that OSV reports as affecting a specific tracked RPM identity.
    """

    __tablename__ = "cve_matches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tracked_rpm_id: Mapped[int] = mapped_column(ForeignKey("tracked_rpms.id"), nullable=False, index=True)
    cve_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    osv_vulnerability_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
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

    tracked_rpm: Mapped["TrackedRpm"] = relationship("TrackedRpm", back_populates="matches")
    affected_releases: Mapped[list["CveMatchRelease"]] = relationship(
        "CveMatchRelease", back_populates="cve_match", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("tracked_rpm_id", "cve_id", name="uq_cve_match"),)


class CveMatchRelease(Base):
    """
    Many-to-many mapping: which Oreon releases currently contain the vulnerable RPM.
    """

    __tablename__ = "cve_match_releases"

    cve_match_id: Mapped[int] = mapped_column(ForeignKey("cve_matches.id"), primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    cve_match: Mapped["CveMatch"] = relationship("CveMatch", back_populates="affected_releases")
    release: Mapped["Release"] = relationship("Release")

