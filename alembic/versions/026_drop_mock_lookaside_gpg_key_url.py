# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later


from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026_drop_mock_gpg_key_url"
down_revision = "025_mock_lookaside_gpg_key_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("mock_environments", "lookaside_gpg_key_url")


def downgrade() -> None:
    op.add_column(
        "mock_environments",
        sa.Column("lookaside_gpg_key_url", sa.String(length=2048), nullable=True),
    )
