"""Workers: indexed token fingerprint for auth (avoid full-table scan + event-loop bcrypt pile-up).

Revision ID: 019
Revises: 018
Create Date: 2026-04-08
"""

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
