"""Add build_attempt_finalizations for idempotent worker result handling.

Revision ID: 012
Revises: 011
Create Date: 2026-03-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "build_attempt_finalizations",
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("finalized_status", sa.String(length=32), nullable=False),
        sa.Column("log_r2_key", sa.String(length=1024), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["build_attempts.id"]),
        sa.PrimaryKeyConstraint("attempt_id"),
    )
    op.create_index(
        op.f("ix_build_attempt_finalizations_attempt_id"),
        "build_attempt_finalizations",
        ["attempt_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("build_attempt_finalizations")

