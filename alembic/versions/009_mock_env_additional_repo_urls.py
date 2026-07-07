# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision = "008"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column(
        "mock_environments",
        sa.Column(
            "additional_repo_urls",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )

def downgrade() -> None:
    op.drop_column("mock_environments", "additional_repo_urls")
