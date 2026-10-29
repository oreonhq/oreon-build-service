# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Release monitor scans can target a side tag layered on the main repos."""

from alembic import op
import sqlalchemy as sa


revision = "045_release_monitor_side_tag"
down_revision = "044_iso_builds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "release_monitor_scans",
        sa.Column("side_tag_id", sa.Integer(), sa.ForeignKey("side_tags.id"), nullable=True),
    )
    op.create_index("ix_release_monitor_scans_side_tag_id", "release_monitor_scans", ["side_tag_id"])


def downgrade() -> None:
    op.drop_index("ix_release_monitor_scans_side_tag_id", table_name="release_monitor_scans")
    op.drop_column("release_monitor_scans", "side_tag_id")
