# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision = "009"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.drop_index(op.f("ix_packages_gitlab_project_id"), table_name="packages")
    op.alter_column(
        "packages",
        "gitlab_project_id",
        new_column_name="github_repository_id",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )
    op.alter_column(
        "packages",
        "gitlab_web_url",
        new_column_name="github_html_url",
        existing_type=sa.String(length=512),
        existing_nullable=True,
    )
    op.create_index(
        op.f("ix_packages_github_repository_id"),
        "packages",
        ["github_repository_id"],
        unique=False,
    )

def downgrade() -> None:
    op.drop_index(op.f("ix_packages_github_repository_id"), table_name="packages")
    op.alter_column(
        "packages",
        "github_repository_id",
        new_column_name="gitlab_project_id",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )
    op.alter_column(
        "packages",
        "github_html_url",
        new_column_name="gitlab_web_url",
        existing_type=sa.String(length=512),
        existing_nullable=True,
    )
    op.create_index(
        op.f("ix_packages_gitlab_project_id"),
        "packages",
        ["gitlab_project_id"],
        unique=False,
    )
