# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later


from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "023_chain_stage_advance_after"
down_revision = "022_build_chains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "build_chains",
        sa.Column("stage_advance_after", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("build_chains", "stage_advance_after")
