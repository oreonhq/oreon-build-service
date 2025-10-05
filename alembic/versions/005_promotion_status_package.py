"""Promotion: status, package_id, decided_at; to_channel nullable.

Revision ID: 005
Revises: 004
Create Date: 2025-03-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("promotions", sa.Column("status", sa.String(32), nullable=True))
    op.execute("UPDATE promotions SET status = 'promoted' WHERE to_channel IS NOT NULL")
    op.execute("UPDATE promotions SET status = 'pending' WHERE status IS NULL")
    op.alter_column("promotions", "status", existing_type=sa.String(32), nullable=False, server_default="pending")

    op.add_column("promotions", sa.Column("package_id", sa.Integer(), nullable=True))
    op.add_column("promotions", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column(
        "promotions",
        "to_channel",
        existing_type=sa.String(32),
        nullable=True,
    )


def downgrade() -> None:
    op.execute("UPDATE promotions SET to_channel = from_channel WHERE to_channel IS NULL")
    op.alter_column("promotions", "to_channel", existing_type=sa.String(32), nullable=False)
    op.drop_column("promotions", "decided_at")
    op.drop_column("promotions", "package_id")
    op.drop_column("promotions", "status")
