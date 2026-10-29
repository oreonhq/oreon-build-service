# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later


from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022_build_chains"
down_revision = "021_attempt_cols_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "build_chains",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("triggered_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "success",
                "failed",
                "cancelled",
                name="buildchainstatus",
            ),
            nullable=False,
        ),
        sa.Column("chain_spec", sa.Text(), nullable=False),
        sa.Column("chain_options", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("stages_json", sa.JSON(), nullable=False),
        sa.Column("active_stage_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage_expected_jobs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage_skip_slots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage_publish_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.ForeignKeyConstraint(["triggered_by_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_build_chains_release_id"), "build_chains", ["release_id"], unique=False)
    op.create_index(op.f("ix_build_chains_status"), "build_chains", ["status"], unique=False)

    op.create_table(
        "build_chain_publish_receipts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("build_chain_id", sa.Integer(), nullable=False),
        sa.Column("build_attempt_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["build_chain_id"], ["build_chains.id"]),
        sa.ForeignKeyConstraint(["build_attempt_id"], ["build_attempts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("build_attempt_id"),
    )
    op.create_index(
        op.f("ix_build_chain_publish_receipts_build_chain_id"),
        "build_chain_publish_receipts",
        ["build_chain_id"],
        unique=False,
    )

    op.add_column("build_jobs", sa.Column("chain_id", sa.Integer(), nullable=True))
    op.add_column("build_jobs", sa.Column("chain_stage_index", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_build_jobs_chain_id"), "build_jobs", ["chain_id"], unique=False)
    op.create_foreign_key(
        "fk_build_jobs_chain_id_build_chains",
        "build_jobs",
        "build_chains",
        ["chain_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_build_jobs_chain_id_build_chains", "build_jobs", type_="foreignkey")
    op.drop_index(op.f("ix_build_jobs_chain_id"), table_name="build_jobs")
    op.drop_column("build_jobs", "chain_stage_index")
    op.drop_column("build_jobs", "chain_id")

    op.drop_index(op.f("ix_build_chain_publish_receipts_build_chain_id"), table_name="build_chain_publish_receipts")
    op.drop_table("build_chain_publish_receipts")

    op.drop_index(op.f("ix_build_chains_status"), table_name="build_chains")
    op.drop_index(op.f("ix_build_chains_release_id"), table_name="build_chains")
    op.drop_table("build_chains")
    op.execute(sa.text("DROP TYPE IF EXISTS buildchainstatus"))
