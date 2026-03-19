"""Add artifacts.is_signed to track GPG signing.

Revision ID: 006
Revises: 005
Create Date: 2025-03-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column("is_signed", sa.Boolean(), nullable=True),
    )
    op.execute("UPDATE artifacts SET is_signed = FALSE WHERE is_signed IS NULL")
    op.alter_column(
        "artifacts",
        "is_signed",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.text("FALSE"),
    )


def downgrade() -> None:
    op.drop_column("artifacts", "is_signed")

