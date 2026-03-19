"""Add build_jobs.branch (default 'dev') for repo layout.

Revision ID: 004
Revises: 003
Create Date: 2025-03-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "build_jobs",
        sa.Column("branch", sa.String(32), nullable=True),
    )
    op.execute("UPDATE build_jobs SET branch = 'dev' WHERE branch IS NULL")
    op.alter_column(
        "build_jobs",
        "branch",
        existing_type=sa.String(32),
        nullable=False,
        server_default="dev",
    )


def downgrade() -> None:
    op.drop_column("build_jobs", "branch")
