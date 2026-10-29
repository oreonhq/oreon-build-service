# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist structured layer dependency blockers."""

from alembic import op
import sqlalchemy as sa

revision = "039_layer_blocking_sources"
down_revision = "038_worker_claim_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "build_jobs",
        sa.Column("blocking_sources", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("build_jobs", "blocking_sources")
