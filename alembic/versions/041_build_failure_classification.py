# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist build failure classification."""

from alembic import op
import sqlalchemy as sa


revision = "041_build_failure_classification"
down_revision = "040_one_running_attempt_per_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("build_attempts", sa.Column("failure_kind", sa.String(length=64), nullable=True))
    op.add_column("build_attempts", sa.Column("failure_fingerprint", sa.String(length=64), nullable=True))
    op.add_column("build_attempts", sa.Column("failure_details", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("build_attempts", "failure_details")
    op.drop_column("build_attempts", "failure_fingerprint")
    op.drop_column("build_attempts", "failure_kind")
