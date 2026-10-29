# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "mock_environments",
        "architecture",
        existing_type=sa.String(32),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "mock_environments",
        "architecture",
        existing_type=sa.String(32),
        nullable=False,
    )
