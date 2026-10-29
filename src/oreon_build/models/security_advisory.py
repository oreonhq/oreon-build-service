# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .account import Account
    from .release import Release

class SecurityAdvisory(Base):

    __tablename__ = "security_advisories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False, index=True)

    cve_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    package_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, index=True)
    package_version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    packages_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

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

    __tablename__ = "security_advisory_releases"

    advisory_id: Mapped[int] = mapped_column(
        ForeignKey("security_advisories.id"), primary_key=True
    )
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    advisory: Mapped["SecurityAdvisory"] = relationship("SecurityAdvisory", back_populates="releases")
    release: Mapped["Release"] = relationship("Release")

