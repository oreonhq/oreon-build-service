# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later


from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "021_attempt_cols_repair"
down_revision = "020_attempt_mock_signing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text("SET LOCAL lock_timeout = '120s'"))
    op.execute(
        sa.text(
            "ALTER TABLE build_attempts "
            "ADD COLUMN IF NOT EXISTS mock_succeeded_at TIMESTAMP WITH TIME ZONE"
        )
    )
    op.execute(
        sa.text(
            """
            DO $f$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'build_attempts'
                      AND column_name = 'signing_status'
                ) THEN
                    ALTER TABLE build_attempts
                        ADD COLUMN signing_status VARCHAR(32) NOT NULL DEFAULT 'not_started';
                    ALTER TABLE build_attempts ALTER COLUMN signing_status DROP DEFAULT;
                END IF;
            END $f$;
            """
        )
    )


def downgrade() -> None:
    pass
