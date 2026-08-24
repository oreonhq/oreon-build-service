"""Indexes for scalable worker claims after bulk layer enqueue."""

from alembic import op
import sqlalchemy as sa


revision = "038_worker_claim_indexes"
down_revision = "037_release_monitor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_build_jobs_claim_queue",
        "build_jobs",
        ["status", sa.text("priority DESC"), "created_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'queued')"),
    )
    op.create_index(
        "ix_build_targets_release_arch_mock",
        "build_targets",
        ["release_id", "architecture", "mock_environment_id"],
        unique=False,
    )
    op.create_index(
        "ix_build_attempts_worker_status",
        "build_attempts",
        ["worker_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_build_attempts_worker_status", table_name="build_attempts")
    op.drop_index("ix_build_targets_release_arch_mock", table_name="build_targets")
    op.drop_index("ix_build_jobs_claim_queue", table_name="build_jobs")
