# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Track automatic maintenance runs."""

from alembic import op
import sqlalchemy as sa


revision = "046_automaint_runs"
down_revision = "045_release_monitor_side_tag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automaint_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("epoch", sa.String(length=64), nullable=False),
        sa.Column("branch", sa.String(length=128), nullable=False),
        sa.Column("release_id", sa.Integer(), sa.ForeignKey("releases.id"), nullable=False),
        sa.Column("side_tag_id", sa.Integer(), sa.ForeignKey("side_tags.id"), nullable=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("rpmbuild_plans.id"), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("packages_json", sa.JSON(), nullable=False),
        sa.Column("fix_round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pr_url", sa.String(length=512), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_automaint_runs_status", "automaint_runs", ["status"])
    op.create_index("ix_automaint_runs_epoch", "automaint_runs", ["epoch"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_automaint_runs_epoch", table_name="automaint_runs")
    op.drop_index("ix_automaint_runs_status", table_name="automaint_runs")
    op.drop_table("automaint_runs")
