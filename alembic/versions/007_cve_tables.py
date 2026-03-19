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


revision: str = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("tracked_rpms"):
        op.create_table(
            "tracked_rpms",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("package_name", sa.String(length=256), nullable=False, index=True),
            sa.Column("rpm_version", sa.String(length=128), nullable=False),
            sa.Column("rpm_release", sa.String(length=128), nullable=False),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
            ),
            sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("package_name", "rpm_version", "rpm_release", name="uq_tracked_rpm"),
        )

    if not insp.has_table("cve_matches"):
        op.create_table(
            "cve_matches",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "tracked_rpm_id",
                sa.Integer(),
                sa.ForeignKey("tracked_rpms.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("cve_id", sa.String(length=64), nullable=False, index=True),
            sa.Column("osv_vulnerability_id", sa.String(length=128), nullable=True),
            sa.Column("upstream_url", sa.String(length=1024), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("recommended_fix", sa.Text(), nullable=True),
            sa.Column("severity", sa.String(length=32), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.UniqueConstraint("tracked_rpm_id", "cve_id", name="uq_cve_match"),
        )

    if not insp.has_table("cve_match_releases"):
        op.create_table(
            "cve_match_releases",
            sa.Column("cve_match_id", sa.Integer(), sa.ForeignKey("cve_matches.id"), primary_key=True),
            sa.Column("release_id", sa.Integer(), sa.ForeignKey("releases.id"), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("cve_match_releases")
    op.drop_table("cve_matches")
    op.drop_table("tracked_rpms")

