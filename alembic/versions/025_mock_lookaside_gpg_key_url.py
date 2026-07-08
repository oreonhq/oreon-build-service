
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "025_mock_lookaside_gpg_key_url"
down_revision = "024_mock_lookaside_repos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mock_environments",
        sa.Column("lookaside_gpg_key_url", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mock_environments", "lookaside_gpg_key_url")
