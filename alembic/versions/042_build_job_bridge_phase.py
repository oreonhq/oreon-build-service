# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Add bridge/native tracking fields to build_jobs."""

from alembic import op
import sqlalchemy as sa


revision = "042_build_job_bridge_phase"
down_revision = "041_build_failure_classification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("build_jobs", sa.Column("build_phase", sa.String(length=32), nullable=False, server_default="phase1_bridge"))
    op.add_column("build_jobs", sa.Column("target_mock_config", sa.String(length=256), nullable=True))
    op.create_index("ix_build_jobs_build_phase", "build_jobs", ["build_phase"])
    op.create_check_constraint(
        "ck_build_jobs_build_phase",
        "build_jobs",
        "build_phase IN ('phase1_bridge', 'phase2_native')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_build_jobs_build_phase", "build_jobs", type_="check")
    op.drop_index("ix_build_jobs_build_phase", table_name="build_jobs")
    op.drop_column("build_jobs", "target_mock_config")
    op.drop_column("build_jobs", "build_phase")
