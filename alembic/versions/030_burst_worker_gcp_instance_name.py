
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "030_burst_gcp_inst"
down_revision = "029_burst_worker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("gcp_instance_name", sa.String(length=253), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "gcp_instance_name")
