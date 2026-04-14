"""Build attempt mock success + controller pipeline status for UI (GPG on controller).

``ALTER TABLE build_attempts`` needs an ACCESS EXCLUSIVE lock. If oreon-api, workers, or any
long transaction touches this table, the migration can block until those finish. Stop the API
and workers (or wait for idle sessions) before running ``alembic upgrade``, or you will sit
there with no output until Postgres gets the lock.

``lock_timeout`` below makes a stuck lock fail after 2 minutes instead of hanging forever.
"""

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
        # Fail fast with a clear lock error instead of blocking without a time limit.
        op.execute(sa.text("SET LOCAL lock_timeout = '120s'"))
        # One ALTER keeps catalog churn minimal vs two separate ADD COLUMN statements.
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
