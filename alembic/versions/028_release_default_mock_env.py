# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later


from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028_release_default_mock_env"
down_revision = "027_rpmbuild_layers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releases",
        sa.Column("default_mock_environment_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_releases_default_mock_environment_id",
        "releases",
        "mock_environments",
        ["default_mock_environment_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_releases_default_mock_environment_id", "releases", type_="foreignkey")
    op.drop_column("releases", "default_mock_environment_id")
