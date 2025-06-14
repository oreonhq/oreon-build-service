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

"""Application configuration from environment."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://oreon:oreon@localhost:5432/oreon_build",
        description="PostgreSQL connection URL (async)",
    )

    # Auth
    secret_key: str = Field(default="change-me-to-a-secure-random-string-min-32-chars")
    jwt_algorithm: str = Field(default="HS256")
    jwt_expire_minutes: int = Field(default=60, ge=5, le=10080)

    # Default admin (first boot)
    admin_username: str = Field(default="admin")
    admin_password: str = Field(default="change-me-secure-password")

    # R2
    r2_account_id: str = Field(default="")
    r2_access_key_id: str = Field(default="")
    r2_secret_access_key: str = Field(default="")
    r2_bucket_name: str = Field(default="oreon-repos")
    r2_endpoint_url: str = Field(default="")
    r2_public_url: Optional[str] = Field(default=None)

    # GitLab
    gitlab_url: str = Field(default="https://gitlab.com")
    gitlab_private_token: Optional[str] = Field(default=None)
    gitlab_group_id: Optional[str] = Field(default=None)

    # Worker
    worker_enrollment_secret: str = Field(default="change-me-worker-secret")
    controller_url: str = Field(default="http://localhost:8000")
    mock_config_dir: str = Field(default="/etc/mock")

    # Webhooks / integrations
    discord_webhook_url: Optional[str] = Field(default=None)
    security_discord_webhook_url: Optional[str] = Field(default=None)
    gitlab_webhook_secret: Optional[str] = Field(default=None)

    # Watchdog (security advisories dashboard)
    watchdog_url: str = Field(default="http://localhost:8001", description="Public base URL for Oreon Watchdog")
    cve_scan_interval_minutes: int = Field(
        default=30,
        ge=1,
        le=1440,
        description="How often Watchdog triggers a CVE scan run (OSV work is still gated by per-RPM cooldown).",
    )
    cve_scan_cooldown_hours: int = Field(default=24, ge=1, le=168, description="Cooldown for re-querying OSV per RPM")
    cve_scan_max_per_run: int = Field(default=50, ge=1, le=500, description="Max tracked RPMs to query per scan run")

    # Signing
    signing_key_id: Optional[str] = Field(default=None)
    signing_key_grip: Optional[str] = Field(default=None)
    gpg_home: str = Field(default="/var/lib/oreon-build/gnupg")

    # Logging
    log_level: str = Field(default="INFO")

    @field_validator("r2_endpoint_url", mode="before")
    @classmethod
    def build_r2_endpoint(cls, v: str, info) -> str:
        if v:
            return v
        aid = info.data.get("r2_account_id") or os.environ.get("R2_ACCOUNT_ID", "")
        if aid:
            return f"https://{aid}.r2.cloudflarestorage.com"
        return ""

    @property
    def sync_database_url(self) -> str:
        """URL for sync driver (Alembic)."""
        if self.database_url.startswith("postgresql+asyncpg://"):
            return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        return self.database_url


def get_settings() -> Settings:
    return Settings()
