"""CVE matches: OSV fixed version + structured resolve reason for backports.

Revision ID: 018
Revises: 017
Create Date: 2026-04-02
"""

from alembic import op
import sqlalchemy as sa

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cve_matches",
        sa.Column("osv_fixed_version", sa.Text(), nullable=True),
    )
    op.add_column(
        "cve_matches",
        sa.Column("handled_reason", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "cve_matches",
        sa.Column("handled_notes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cve_matches", "handled_notes")
    op.drop_column("cve_matches", "handled_reason")
    op.drop_column("cve_matches", "osv_fixed_version")
