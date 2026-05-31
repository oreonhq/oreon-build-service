"""Add packages_json to security_advisories."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "031_adv_packages_json"
down_revision = "030_burst_gcp_inst"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("security_advisories", sa.Column("packages_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("security_advisories", "packages_json")
