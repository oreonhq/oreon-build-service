# Oreon Build Service
# Copyright (C) 2026 Oreon HQ
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Store last seen GitHub push HEAD per repo branch for offline catch-up."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_push_cursors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "repository_id",
            "branch",
            name="uq_github_push_cursors_repository_branch",
        ),
    )
    op.create_index(
        op.f("ix_github_push_cursors_repository_id"),
        "github_push_cursors",
        ["repository_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_github_push_cursors_repository_id"), table_name="github_push_cursors")
    op.drop_table("github_push_cursors")
