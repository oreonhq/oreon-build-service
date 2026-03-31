"""Distributed lock rows for RPM signing/finalize to avoid concurrent overwrite races.

Revision ID: 015
Revises: 014
Create Date: 2026-03-30
"""

from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rpm_complete_locks",
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("r2_key", sa.String(length=1024), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("attempt_id", "r2_key"),
        sa.UniqueConstraint("attempt_id", "r2_key", name="ux_rpm_complete_locks_attempt_r2_key"),
    )
    op.create_index(
        op.f("ix_rpm_complete_locks_expires_at"),
        "rpm_complete_locks",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_rpm_complete_locks_expires_at"), table_name="rpm_complete_locks")
    op.drop_table("rpm_complete_locks")

