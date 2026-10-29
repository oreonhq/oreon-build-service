# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later


from alembic import op
import sqlalchemy as sa

revision = "034_build_job_options"
down_revision = "032_default_distgit_branch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("build_jobs", sa.Column("job_options", sa.JSON(), nullable=True))
    op.add_column("build_attempts", sa.Column("build_profile", sa.String(length=32), nullable=True))
    op.add_column("build_attempts", sa.Column("rpmbuild_defines", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("build_attempts", "rpmbuild_defines")
    op.drop_column("build_attempts", "build_profile")
    op.drop_column("build_jobs", "job_options")
