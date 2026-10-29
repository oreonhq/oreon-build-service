# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later


from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "020_attempt_mock_signing"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '120s'"))
        op.execute(
            sa.text(
                """
                ALTER TABLE build_attempts
                    ADD COLUMN mock_succeeded_at TIMESTAMP WITH TIME ZONE NULL,
                    ADD COLUMN signing_status VARCHAR(32) NOT NULL DEFAULT 'not_started'
                """
            )
        )
        op.execute(sa.text("ALTER TABLE build_attempts ALTER COLUMN signing_status DROP DEFAULT"))
    else:
        op.add_column(
            "build_attempts",
            sa.Column("mock_succeeded_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            "build_attempts",
            sa.Column(
                "signing_status",
                sa.String(32),
                nullable=False,
                server_default="not_started",
            ),
        )
        op.alter_column("build_attempts", "signing_status", server_default=None)


def downgrade() -> None:
    op.drop_column("build_attempts", "signing_status")
    op.drop_column("build_attempts", "mock_succeeded_at")
