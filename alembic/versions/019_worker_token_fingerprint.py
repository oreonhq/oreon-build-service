# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later


from alembic import op
import sqlalchemy as sa

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column("token_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_workers_token_fingerprint",
        "workers",
        ["token_fingerprint"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_workers_token_fingerprint", table_name="workers")
    op.drop_column("workers", "token_fingerprint")
