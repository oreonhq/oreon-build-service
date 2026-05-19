"""Add lookaside_repo_urls to mock_environments (bootstrap/Fedora lookaside)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "024_mock_lookaside_repos"
down_revision = "023_chain_stage_advance_after"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mock_environments",
        sa.Column(
            "lookaside_repo_urls",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.alter_column("mock_environments", "lookaside_repo_urls", server_default=None)


def downgrade() -> None:
    op.drop_column("mock_environments", "lookaside_repo_urls")
