"""Add burst worker fields to workers table."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "029_burst_worker"
down_revision = "028_release_default_mock_env"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("is_burst_worker", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("workers", sa.Column("burst_whitelist", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("workers", sa.Column("gcp_zone", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "gcp_zone")
    op.drop_column("workers", "burst_whitelist")
    op.drop_column("workers", "is_burst_worker")
