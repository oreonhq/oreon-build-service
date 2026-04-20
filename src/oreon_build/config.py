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
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator
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
    database_reclaim_url: Optional[str] = Field(
        default=None,
        description=(
            "Optional async Postgres URL used only by scripts/reclaim_stuck_publish_tasks.py when "
            "DATABASE_URL cannot connect because max_connections is saturated for that role. "
            "Use the same database as postgresql+asyncpg://postgres:...@host:5432/oreon_build so reclaim "
            "can use reserved superuser connection slots. Env DATABASE_RECLAIM_URL."
        ),
    )
    database_use_null_pool: bool = Field(
        default=True,
        description=(
            "If True, use SQLAlchemy NullPool (DATABASE_USE_NULL_POOL): open a real Postgres connection "
            "for each checked-out session and close it on return. No idle client-side pool per process, "
            "so multiple API/Watchdog/scheduler processes do not multiply reserved connections. "
            "Set False and tune DATABASE_POOL_* when you have a large max_connections and want pooling."
        ),
    )
    database_pool_size: int = Field(
        default=14,
        ge=1,
        description="Per-process SQLAlchemy QueuePool pool_size (DATABASE_POOL_SIZE).",
    )
    database_max_overflow: int = Field(
        default=16,
        ge=0,
        description="Per-process QueuePool max_overflow beyond pool_size (DATABASE_MAX_OVERFLOW).",
    )
    database_pool_max_connections_per_process: int = Field(
        default=30,
        ge=0,
        description=(
            "Ceiling on pool_size+max_overflow for this OS process (DATABASE_POOL_MAX_CONNECTIONS_PER_PROCESS). "
            "API and Watchdog each load oreon_build.db.session and each get a full pool, so two processes use twice this at peak. "
            "0 disables clamping (you must keep Postgres max_connections above your own totals)."
        ),
    )
    database_pool_timeout: int = Field(
        default=30,
        ge=0,
        description=(
            "Seconds to wait when acquiring a pooled connection (DATABASE_POOL_TIMEOUT). "
            "0 is passed through to SQLAlchemy as configured there."
        ),
    )
    database_idle_in_transaction_timeout_seconds: int = Field(
        default=120,
        ge=0,
        description=(
            "Postgres idle_in_transaction_session_timeout in ms for new connections (0 disables). "
            "DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS."
        ),
    )
    database_application_name: str = Field(
        default="oreon-build-service",
        min_length=1,
        max_length=128,
        description="PostgreSQL application_name tag used in pg_stat_activity.",
    )

    @field_validator("database_use_null_pool", mode="before")
    @classmethod
    def _coerce_database_use_null_pool(cls, v: object) -> bool:
        if v is None or v is Ellipsis:
            return True
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in ("0", "false", "no", "off"):
            return False
        if s in ("1", "true", "yes", "on"):
            return True
        return bool(v)

    @model_validator(mode="after")
    def _clamp_database_pool_to_postgres_safe_total(self) -> Settings:
        if self.database_use_null_pool:
            return self
        cap = int(self.database_pool_max_connections_per_process)
        if cap <= 0:
            return self
        ps = int(self.database_pool_size)
        mo = int(self.database_max_overflow)
        if ps + mo <= cap:
            return self
        if ps > cap:
            new_ps, new_mo = cap, 0
        else:
            new_ps, new_mo = ps, cap - ps
        warnings.warn(
            f"Database pool clamped to pool_size={new_ps} max_overflow={new_mo} "
            f"(was pool_size={ps} max_overflow={mo}) because "
            f"database_pool_max_connections_per_process={cap}. "
            "Each Oreon process that imports the DB engine holds its own pool. "
            "Raise Postgres max_connections, lower other clients, or set "
            "DATABASE_POOL_MAX_CONNECTIONS_PER_PROCESS=0 only if you sized Postgres yourself.",
            UserWarning,
            stacklevel=1,
        )
        self.database_pool_size = max(1, new_ps)
        self.database_max_overflow = max(0, new_mo)
        return self

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
    oreon_repo_tmpdir: Optional[str] = Field(
        default=None,
        description=(
            "Directory for createrepo temp dirs during R2 repodata refresh. "
            "If unset, defaults to ~/.oreon/repo-tmp (not tmpfs /tmp). Same as env OREON_REPO_TMPDIR."
        ),
    )
    repo_publish_max_concurrent: int = Field(
        default=2,
        ge=1,
        le=32,
        description=(
            "Max concurrent repo-publish jobs (R2 sync + createrepo) per API process. "
            "Build completions enqueue PublishTask rows; the background loop runs them under this cap "
            "so many finished builds do not starve the event loop and default thread pool."
        ),
    )
    repo_publish_poll_batch: int = Field(
        default=1,
        ge=1,
        le=50,
        description="How many pending PublishTask rows to dequeue per poll cycle (each still obeys repo_publish_max_concurrent).",
    )
    publish_task_stale_running_minutes: int = Field(
        default=240,
        ge=0,
        le=10080,
        description=(
            "If a PublishTask stays status=running longer than this (crash mid-repodata, hung createrepo), "
            "the stale-build sweep resets it to pending so publish can retry. 0 disables. "
            "PUBLISH_TASK_STALE_RUNNING_MINUTES."
        ),
    )

    # GitHub (API + webhooks)
    github_api_url: str = Field(default="https://api.github.com", description="GitHub REST API base URL")
    github_token: Optional[str] = Field(default=None, description="Fine-grained or classic PAT for API calls")
    github_org: Optional[str] = Field(default=None, description="Org login for /api/github/repos listing")
    github_webhook_secret: Optional[str] = Field(
        default=None,
        description="Secret for X-Hub-Signature-256 on POST /api/github/webhook",
    )
    github_catchup_interval_seconds: int = Field(
        default=600,
        ge=0,
        le=86400,
        description=(
            "Background interval to run GitHub compare vs github_push_cursors (missed webhooks). "
            "0 disables. Needs GITHUB_TOKEN."
        ),
    )
    github_catchup_bootstrap_commits: int = Field(
        default=0,
        ge=0,
        le=500,
        description=(
            "On first cursor creation, backfill up to this many recent commits via compare. "
            "0 keeps old baseline-only behavior."
        ),
    )
    github_catchup_max_jobs_per_cycle: int = Field(
        default=10,
        ge=1,
        le=5000,
        description="Hard cap on jobs enqueued by one catch-up cycle to prevent flood/spam.",
    )

    # Worker
    worker_enrollment_secret: str = Field(default="change-me-worker-secret")
    controller_url: str = Field(default="http://localhost:8000")
    mock_config_dir: str = Field(default="/etc/mock")
    worker_offline_after_seconds: int = Field(
        default=120,
        ge=0,
        le=86400,
        description=(
            "List/get worker API reports state offline when last_heartbeat is older than this many "
            "seconds (idle/busy in the DB are stale). Workers heartbeat about every 30s. 0 uses DB state only."
        ),
    )
    # Webhooks / integrations
    discord_webhook_url: Optional[str] = Field(default=None)
    security_discord_webhook_url: Optional[str] = Field(default=None)

    # Watchdog (security advisories dashboard)
    watchdog_url: str = Field(default="http://localhost:8001", description="Public base URL for Oreon Watchdog")
    cve_scan_interval_minutes: int = Field(
        default=30,
        ge=1,
        le=1440,
        description="How often Watchdog refreshes the tracked RPM inventory from build artifacts (no external CVE APIs).",
    )
    cve_scan_omit_subpackage_suffixes: str = Field(
        default="debuginfo,debugsource,doc,help,static",
        description="Comma-separated name suffixes to skip (e.g. foo-debuginfo). Empty string disables.",
    )

    # Signing (controller only — workers upload unsigned RPMs; controller signs then pushes to R2)
    signing_key_id: Optional[str] = Field(default=None)
    signing_key_grip: Optional[str] = Field(default=None)
    gpg_home: str = Field(default="/var/lib/oreon-build/gnupg")

    # Secure Boot / PE signing: controller holds NSS + rules; workers fetch a short-lived bundle
    # (GET /api/worker/pesign-bundle) and run pesign before upload. Controller only GPG-signs RPMs.
    # Workers need pesign, rpmrebuild, file, bash on PATH.
    pesign_nss_dir: str = Field(
        default="/etc/pki/pesign",
        description="NSS database directory for pesign (-n)",
    )
    pesign_certificate_nickname: Optional[str] = Field(
        default=None,
        description=(
            "Default NSS certificate nickname (-c) when no pesign_rules_json rule matches but "
            "pesign_package_name_regex matches. Also enables single-cert mode if rules are empty."
        ),
    )
    pesign_rules_json: Optional[str] = Field(
        default=None,
        description=(
            'Optional JSON array of ordered rules: '
            '{"name_regex": "^grub2-efi.*", "nickname": "signer", "arch_regex": "^(s390x)$"} '
            "(arch_regex optional). First match wins; use before a global fallback nickname."
        ),
    )
    pesign_token: Optional[str] = Field(
        default=None,
        description="Optional PKCS#11 token name for pesign (-t)",
    )
    pesign_aarch64_qemu_prefix: Optional[str] = Field(
        default=None,
        description=(
            "Shell prefix used to run rpmrebuild for cross-arch aarch64 payload repacks "
            "(example: 'qemu-aarch64-static -L /path/to/aarch64-rootfs'). "
            "Required when signing aarch64 RPMs on non-aarch64 hosts. "
            "The -L path should be a full AArch64 root (bash or sh inside), not only /usr/aarch64-linux-gnu."
        ),
    )
    pesign_aarch64_guest_shell: Optional[str] = Field(
        default=None,
        description=(
            "Host path to an AArch64 ELF sh or bash used as qemu guest interpreter when -L "
            "does not contain bin/sh (e.g. libc-only sysroot). Must match libraries implied by the same rootfs."
        ),
    )
    pesign_package_name_regex: str = Field(
        default=r"^(kernel.*|shim.*|fwupd.*|grub2-efi.*)$",
        description="RPM %%{NAME} must full-match this regex (case-insensitive) for pesign",
    )
    pesign_strict: bool = Field(
        default=False,
        description="If true, upload fails when a matching RPM cannot be pesign-repacked",
    )

    # Worker → controller RPM upload (single artifact size cap, MiB). Default 51200 (= 50 GiB).
    # Keep <= any reverse-proxy client_max_body_size on the origin. HTTP 413 with no JSON detail
    # is often nginx or another hop before FastAPI. Cloudflare Tunnel (cloudflared) does not add the
    # same upload cap as orange-cloud HTTP proxy, but whatever cloudflared forwards to must allow
    # large POST bodies (e.g. nginx on loopback in front of uvicorn).
    max_worker_rpm_upload_mib: int = Field(default=51200, ge=16, le=102400)
    worker_rpm_presign_put_expires_seconds: int = Field(
        default=7200,
        ge=600,
        le=86400,
        description="Presigned PUT to R2 TTL for worker direct RPM uploads (bypasses small POST limits on tunnels).",
    )

    # Stuck builds: jobs stay RUNNING until the worker posts a result; sweep marks them failed if
    # the attempt is too old or the worker has not heartbeated (see services/stale_builds.py).
    build_stale_running_minutes: int = Field(
        default=2880,
        ge=0,
        le=43200,
        description="Fail RUNNING attempts older than this many minutes (0 = disable). Default 48h; raise for very long kernel builds.",
    )
    build_stale_worker_heartbeat_minutes: int = Field(
        default=120,
        ge=0,
        le=1440,
        description="Fail RUNNING attempts whose worker last_heartbeat is older than this (0 = disable).",
    )
    build_stale_pending_minutes: int = Field(
        default=1440,
        ge=0,
        description=(
            "Fail PENDING/QUEUED jobs that have been waiting this many minutes without being claimed "
            "by a worker (0 = disable). Default 24h; jobs sitting longer usually mean no worker matches "
            "the requested architecture or the worker pool is down."
        ),
    )
    build_stale_pending_require_no_eligible_worker: bool = Field(
        default=False,
        description=(
            "If true, only fail stuck PENDING/QUEUED jobs when no worker matching the job's "
            "architecture has heartbeated within worker_offline_after_seconds. If false, fail "
            "purely by age (safer for small pools where workers come and go)."
        ),
    )
    stale_build_sweep_interval_seconds: int = Field(
        default=300,
        ge=60,
        le=86400,
        description="How often the API runs the stale-build sweeper in the background.",
    )

    # Logging
    log_level: str = Field(default="INFO")

    @field_validator("github_token", mode="before")
    @classmethod
    def github_token_strip_empty(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """One cached instance per process (API, Watchdog, CLI each have their own cache)."""
    return Settings()
