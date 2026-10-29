# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later


from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                FIRST_VALUE(id) OVER (
                    PARTITION BY release_id, architecture, mock_environment_id
                    ORDER BY id
                ) AS canonical_id
            FROM build_targets
        ),
        to_remap AS (
            SELECT id, canonical_id FROM ranked WHERE id != canonical_id
        )
        UPDATE build_jobs bj
        SET target_id = tr.canonical_id
        FROM to_remap tr
        WHERE bj.target_id = tr.id
        """
    )
    op.execute(
        """
        DELETE FROM build_targets bt
        USING (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY release_id, architecture, mock_environment_id
                        ORDER BY id
                    ) AS rn
                FROM build_targets
            ) x
            WHERE x.rn > 1
        ) d
        WHERE bt.id = d.id
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_build_targets_release_arch_mock_nonnull
        ON build_targets (release_id, architecture, mock_environment_id)
        WHERE mock_environment_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_build_targets_release_arch_mock_null
        ON build_targets (release_id, architecture)
        WHERE mock_environment_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_build_targets_release_arch_mock_null")
    op.execute("DROP INDEX IF EXISTS uq_build_targets_release_arch_mock_nonnull")
