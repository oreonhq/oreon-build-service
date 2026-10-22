from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from oreon_build.db.base import Base


class RpmScanFinding(Base):
    __tablename__ = "rpm_scan_findings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    build_job_id: Mapped[int] = mapped_column(ForeignKey("build_jobs.id"), nullable=False, index=True)
    attempt_id: Mapped[Optional[int]] = mapped_column(ForeignKey("build_attempts.id"), nullable=True, index=True)
    engine: Mapped[str] = mapped_column(String(16), nullable=False)
    rule: Mapped[str] = mapped_column(String(256), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
