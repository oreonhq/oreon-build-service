# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""One running build attempt per job."""

from alembic import op
import sqlalchemy as sa


revision = "040_one_running_attempt_per_job"
down_revision = "039_layer_blocking_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY build_job_id
                    ORDER BY id
                ) AS rn
            FROM build_attempts
            WHERE status = 'running'
        )
        UPDATE build_attempts AS ba
        SET
            status = 'cancelled',
            finished_at = COALESCE(ba.finished_at, NOW()),
            error_message = 'duplicate concurrent attempt'
        FROM ranked
        WHERE ba.id = ranked.id
          AND ranked.rn > 1
        """
    )
    op.create_index(
        "ux_build_attempts_one_running_per_job",
        "build_attempts",
        ["build_job_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ux_build_attempts_one_running_per_job", table_name="build_attempts")
