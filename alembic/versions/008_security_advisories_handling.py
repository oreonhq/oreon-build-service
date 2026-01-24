# Oreon Build Service
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# Alembic identifiers
revision: str = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def _table_has_column(insp: sa.Inspector, table_name: str, column_name: str) -> bool:
    try:
        cols = insp.get_columns(table_name)
    except Exception:
        return False
    return any(c.get("name") == column_name for c in cols)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # --- cve_matches: add handling columns (if not already present)
    if insp.has_table("cve_matches"):
        if not _table_has_column(insp, "cve_matches", "is_handled"):
            op.add_column(
                "cve_matches",
                sa.Column("is_handled", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            )
            op.execute("UPDATE cve_matches SET is_handled = FALSE WHERE is_handled IS NULL")
        if not _table_has_column(insp, "cve_matches", "handled_at"):
            op.add_column("cve_matches", sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True))
        if not _table_has_column(insp, "cve_matches", "handled_by_account_id"):
            op.add_column(
                "cve_matches",
                sa.Column("handled_by_account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
            )

    # --- custom advisories: tables + m2m mapping
    if not insp.has_table("security_advisories"):
        op.create_table(
            "security_advisories",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("title", sa.String(length=256), nullable=False, index=True),
            sa.Column("cve_id", sa.String(length=64), nullable=True, index=True),
            sa.Column("package_name", sa.String(length=256), nullable=True, index=True),
            sa.Column("package_version", sa.String(length=128), nullable=True),
            sa.Column("upstream_url", sa.String(length=1024), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("recommended_fix", sa.Text(), nullable=True),
            sa.Column("severity", sa.String(length=32), nullable=True),
            sa.Column("is_handled", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("handled_by_account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
            sa.Column("created_by_account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
        )

    if not insp.has_table("security_advisory_releases"):
        op.create_table(
            "security_advisory_releases",
            sa.Column("advisory_id", sa.Integer(), sa.ForeignKey("security_advisories.id"), primary_key=True),
            sa.Column("release_id", sa.Integer(), sa.ForeignKey("releases.id"), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        )


def downgrade() -> None:
    # Keeping downgrade simple since we primarily use upgrade in this project.
    # If you need downgrade, ask and we will implement it safely.
    pass

