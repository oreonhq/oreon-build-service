"""Initial schema: accounts, packages, builds, releases, workers, repos, etc.

Revision ID: 001
Revises:
Create Date: 2025-01-01 00:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Enum("admin", "maintainer", name="rolename"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(op.f("ix_roles_name"), "roles", ["name"], unique=True)

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index(op.f("ix_accounts_username"), "accounts", ["username"], unique=True)

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_id", sa.String(128), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_logs_action"), "audit_logs", ["action"], unique=False)
    op.create_index(op.f("ix_audit_logs_resource_type"), "audit_logs", ["resource_type"], unique=False)
    op.create_index(op.f("ix_audit_logs_resource_id"), "audit_logs", ["resource_id"], unique=False)

    op.create_table(
        "packages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("gitlab_project_id", sa.Integer(), nullable=True),
        sa.Column("gitlab_web_url", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_packages_name"), "packages", ["name"], unique=False)
    op.create_index(op.f("ix_packages_gitlab_project_id"), "packages", ["gitlab_project_id"], unique=False)

    op.create_table(
        "releases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("releasename", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("architectures", sa.String(256), nullable=False),
        sa.Column("default_channel", sa.Enum("dev", "testing", "stable", name="releasechannel"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("releasename"),
    )
    op.create_index(op.f("ix_releases_releasename"), "releases", ["releasename"], unique=True)

    op.create_table(
        "release_repos",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("baseurl", sa.String(1024), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "mock_environments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("config_name", sa.String(128), nullable=False),
        sa.Column("architecture", sa.String(32), nullable=False),
        sa.Column("config_content", sa.Text(), nullable=True),
        sa.Column("is_available", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "workers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("state", sa.Enum("idle", "busy", "unhealthy", "offline", "draining", name="workerstate"), nullable=False),
        sa.Column("architecture", sa.String(32), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_build_attempt_id", sa.Integer(), nullable=True),
        sa.Column("capabilities", sa.Text(), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(op.f("ix_workers_name"), "workers", ["name"], unique=True)
    op.create_index(op.f("ix_workers_state"), "workers", ["state"], unique=False)

    op.create_table(
        "package_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("release_tag", sa.String(64), nullable=True),
        sa.Column("git_sha", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["package_id"], ["packages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_package_versions_git_sha"), "package_versions", ["git_sha"], unique=False)

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_version_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("url", sa.String(1024), nullable=True),
        sa.Column("content_hash", sa.String(128), nullable=True),
        sa.Column("r2_key", sa.String(1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["package_version_id"], ["package_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "build_targets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("architecture", sa.String(32), nullable=False),
        sa.Column("mock_environment_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.ForeignKeyConstraint(["mock_environment_id"], ["mock_environments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "build_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("package_version_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.Enum("pending", "queued", "running", "success", "failed", "cancelled", "skipped", name="buildstatus"), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("triggered_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["package_id"], ["packages.id"]),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.ForeignKeyConstraint(["target_id"], ["build_targets.id"]),
        sa.ForeignKeyConstraint(["package_version_id"], ["package_versions.id"]),
        sa.ForeignKeyConstraint(["triggered_by_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_build_jobs_status"), "build_jobs", ["status"], unique=False)

    op.create_table(
        "build_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("build_job_id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Integer(), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("pending", "queued", "running", "success", "failed", "cancelled", "skipped", name="buildstatus"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("log_r2_key", sa.String(1024), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["build_job_id"], ["build_jobs.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("build_attempt_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("r2_key", sa.String(1024), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("checksum_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["build_attempt_id"], ["build_attempts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "repositories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("architecture", sa.String(32), nullable=False),
        sa.Column("r2_prefix", sa.String(512), nullable=False),
        sa.Column("last_compose_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_repositories_channel"), "repositories", ["channel"], unique=False)

    op.create_table(
        "repository_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=False),
        sa.Column("r2_prefix", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_repository_snapshots_snapshot_id"), "repository_snapshots", ["snapshot_id"], unique=False)

    op.create_table(
        "promotions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("from_channel", sa.String(32), nullable=False),
        sa.Column("to_channel", sa.String(32), nullable=False),
        sa.Column("package_name", sa.String(256), nullable=True),
        sa.Column("build_job_id", sa.Integer(), nullable=True),
        sa.Column("promoted_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["promoted_by_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "signing_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=True),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("architecture", sa.String(32), nullable=False),
        sa.Column("status", sa.Enum("pending", "running", "success", "failed", name="signingjobstatus"), nullable=False),
        sa.Column("r2_prefix", sa.String(512), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("cron_expression", sa.String(128), nullable=True),
        sa.Column("config", sa.Text(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("schedules")
    op.drop_table("signing_jobs")
    op.drop_table("promotions")
    op.drop_table("repository_snapshots")
    op.drop_table("repositories")
    op.drop_table("artifacts")
    op.drop_table("build_attempts")
    op.drop_table("build_jobs")
    op.drop_table("build_targets")
    op.drop_table("sources")
    op.drop_table("package_versions")
    op.drop_table("workers")
    op.drop_table("mock_environments")
    op.drop_table("release_repos")
    op.drop_table("releases")
    op.drop_table("packages")
    op.drop_table("audit_logs")
    op.drop_table("accounts")
    op.drop_table("roles")
    op.execute("DROP TYPE IF EXISTS buildstatus")
    op.execute("DROP TYPE IF EXISTS workerstate")
    op.execute("DROP TYPE IF EXISTS releasechannel")
    op.execute("DROP TYPE IF EXISTS signingjobstatus")
    op.execute("DROP TYPE IF EXISTS rolename")
