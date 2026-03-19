"""Add preferred_mock_environment_id to packages.

Revision ID: 002
Revises: 001
Create Date: 2025-03-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "packages",
        sa.Column("preferred_mock_environment_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_packages_preferred_mock_environment_id_mock_environments"),
        "packages",
        "mock_environments",
        ["preferred_mock_environment_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_packages_preferred_mock_environment_id_mock_environments"),
        "packages",
        type_="foreignkey",
    )
    op.drop_column("packages", "preferred_mock_environment_id")
