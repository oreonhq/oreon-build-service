"""Add rpmbuild plans and BuildJob layer metadata."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "027_rpmbuild_layers"
down_revision = "026_drop_mock_gpg_key_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rpmbuild_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("releasename", sa.String(length=128), nullable=False),
        sa.Column("repo_branch", sa.String(length=64), nullable=False),
        sa.Column("distgit_url", sa.String(length=1024), nullable=False),
        sa.Column("distgit_branch", sa.String(length=128), nullable=False),
        sa.Column("seed_mode", sa.String(length=32), nullable=False),
        sa.Column("seed_packages_json", sa.JSON(), nullable=False),
        sa.Column("layers_json", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "waiting_settle",
                "success",
                "failed",
                "cancelled",
                name="rpmbuildplanstatus",
            ),
            nullable=False,
        ),
        sa.Column("current_open_layer", sa.Integer(), nullable=False),
        sa.Column("settle_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rpmbuild_plans_release_id"), "rpmbuild_plans", ["release_id"], unique=False)
    op.create_index(op.f("ix_rpmbuild_plans_releasename"), "rpmbuild_plans", ["releasename"], unique=False)
    op.create_index(op.f("ix_rpmbuild_plans_status"), "rpmbuild_plans", ["status"], unique=False)

    op.add_column("build_jobs", sa.Column("rpmbuild_plan_id", sa.Integer(), nullable=True))
    op.add_column("build_jobs", sa.Column("rpmbuild_layer_index", sa.Integer(), nullable=True))
    op.add_column("build_jobs", sa.Column("rpmbuild_wave_index", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_build_jobs_rpmbuild_plan_id"), "build_jobs", ["rpmbuild_plan_id"], unique=False)
    op.create_index(
        op.f("ix_build_jobs_rpmbuild_layer_index"), "build_jobs", ["rpmbuild_layer_index"], unique=False
    )
    op.create_foreign_key(
        "fk_build_jobs_rpmbuild_plan_id",
        "build_jobs",
        "rpmbuild_plans",
        ["rpmbuild_plan_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_build_jobs_rpmbuild_plan_id", "build_jobs", type_="foreignkey")
    op.drop_index(op.f("ix_build_jobs_rpmbuild_layer_index"), table_name="build_jobs")
    op.drop_index(op.f("ix_build_jobs_rpmbuild_plan_id"), table_name="build_jobs")
    op.drop_column("build_jobs", "rpmbuild_wave_index")
    op.drop_column("build_jobs", "rpmbuild_layer_index")
    op.drop_column("build_jobs", "rpmbuild_plan_id")

    op.drop_index(op.f("ix_rpmbuild_plans_status"), table_name="rpmbuild_plans")
    op.drop_index(op.f("ix_rpmbuild_plans_releasename"), table_name="rpmbuild_plans")
    op.drop_index(op.f("ix_rpmbuild_plans_release_id"), table_name="rpmbuild_plans")
    op.drop_table("rpmbuild_plans")
    sa.Enum(name="rpmbuildplanstatus").drop(op.get_bind(), checkfirst=False)
