# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later


from alembic import op
import sqlalchemy as sa

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publish_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="repo_publish"),
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("build_job_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["build_attempts.id"]),
        sa.ForeignKeyConstraint(["build_job_id"], ["build_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_unique_constraint("ux_publish_tasks_attempt_id", "publish_tasks", ["attempt_id"])
    op.create_index(op.f("ix_publish_tasks_build_job_id"), "publish_tasks", ["build_job_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_publish_tasks_build_job_id"), table_name="publish_tasks")
    op.drop_table("publish_tasks")

