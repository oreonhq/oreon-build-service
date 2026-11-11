# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from alembic import op
import sqlalchemy as sa


revision = "048_release_monitor_taiga"
down_revision = "047_rpm_scan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "release_monitor_taiga_issues",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("releasename", sa.String(length=128), nullable=False),
        sa.Column("package_name", sa.String(length=256), nullable=False),
        sa.Column("latest_version", sa.String(length=256), nullable=False),
        sa.Column("taiga_issue_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("releasename", "package_name", "latest_version", name="uq_release_monitor_taiga_pkg_ver"),
    )
    op.create_index("ix_release_monitor_taiga_issues_releasename", "release_monitor_taiga_issues", ["releasename"])
    op.create_index("ix_release_monitor_taiga_issues_package_name", "release_monitor_taiga_issues", ["package_name"])


def downgrade() -> None:
    op.drop_index("ix_release_monitor_taiga_issues_package_name", table_name="release_monitor_taiga_issues")
    op.drop_index("ix_release_monitor_taiga_issues_releasename", table_name="release_monitor_taiga_issues")
    op.drop_table("release_monitor_taiga_issues")
