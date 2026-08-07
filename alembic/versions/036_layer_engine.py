
from alembic import op
import sqlalchemy as sa

revision = "036_layer_engine"
down_revision = "035_side_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    layerplanstatus = sa.Enum(
        "pending", "running", "success", "failed", "cancelled", name="layerplanstatus"
    )
    layerplanstatus.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "layer_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("triggered_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            layerplanstatus,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("root_packages", sa.JSON(), nullable=False),
        sa.Column("plan_options", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("side_tag_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.ForeignKeyConstraint(["triggered_by_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["side_tag_id"], ["side_tags.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_layer_plans_release_id", "layer_plans", ["release_id"])
    op.create_index("ix_layer_plans_status", "layer_plans", ["status"])
    op.create_index("ix_layer_plans_side_tag_id", "layer_plans", ["side_tag_id"])

    # Carry over old build_chains rows so build_jobs.layer_plan_id (copied
    # from chain_id below) still points at something real. Dropped
    # stage/spec fields have no equivalent, so root_packages is best-effort
    # from chain_spec - it's only used for display on these old rows.
    op.execute(
        """
        INSERT INTO layer_plans (
            id, release_id, triggered_by_id, status, root_packages,
            plan_options, error_message, side_tag_id, created_at, updated_at
        )
        SELECT
            id, release_id, triggered_by_id, status::text::layerplanstatus,
            to_jsonb(
                array_remove(
                    string_to_array(
                        regexp_replace(coalesce(chain_spec, ''), '[:\n]', ' ', 'g'),
                        ' '
                    ),
                    ''
                )
            ),
            coalesce(chain_options, '{}'::json),
            error_message, side_tag_id, created_at, updated_at
        FROM build_chains
        """
    )
    op.execute(
        "SELECT setval('layer_plans_id_seq', COALESCE((SELECT MAX(id) FROM layer_plans), 1))"
    )

    op.add_column("build_jobs", sa.Column("layer_plan_id", sa.Integer(), nullable=True))
    op.add_column("build_jobs", sa.Column("layer_index", sa.Integer(), nullable=True))
    op.add_column("build_jobs", sa.Column("blocked_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_build_jobs_layer_plan_id", "build_jobs", "layer_plans", ["layer_plan_id"], ["id"]
    )
    op.create_index("ix_build_jobs_layer_plan_id", "build_jobs", ["layer_plan_id"])

    op.execute(
        "UPDATE build_jobs SET layer_plan_id = chain_id, layer_index = chain_stage_index "
        "WHERE chain_id IS NOT NULL"
    )

    op.create_table(
        "layer_job_blockers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("build_job_id", sa.Integer(), nullable=False),
        sa.Column("blocking_source", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["build_job_id"], ["build_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_layer_job_blockers_build_job_id", "layer_job_blockers", ["build_job_id"])
    op.create_index("ix_layer_job_blockers_blocking_source", "layer_job_blockers", ["blocking_source"])
    op.create_index(
        "ix_layer_job_blockers_source_job", "layer_job_blockers", ["blocking_source", "build_job_id"]
    )

    op.drop_constraint("fk_build_jobs_side_tag_id", "build_jobs", type_="foreignkey")
    op.drop_index("ix_build_jobs_side_tag_id", table_name="build_jobs")
    op.drop_index("ix_build_jobs_chain_id", table_name="build_jobs")
    op.drop_column("build_jobs", "chain_id")
    op.drop_column("build_jobs", "chain_stage_index")
    op.create_index("ix_build_jobs_side_tag_id", "build_jobs", ["side_tag_id"])
    op.create_foreign_key(
        "fk_build_jobs_side_tag_id", "build_jobs", "side_tags", ["side_tag_id"], ["id"]
    )

    op.drop_table("build_chain_publish_receipts")
    op.drop_table("build_chains")

    op.execute("ALTER TYPE buildstatus ADD VALUE IF NOT EXISTS 'blocked'")


def downgrade() -> None:
    buildchainstatus = sa.Enum(
        "pending", "running", "success", "failed", "cancelled", name="buildchainstatus"
    )
    buildchainstatus.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "build_chains",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("triggered_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            buildchainstatus,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("chain_spec", sa.Text(), nullable=False),
        sa.Column("chain_options", sa.JSON(), nullable=False),
        sa.Column("stages_json", sa.JSON(), nullable=False),
        sa.Column("active_stage_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage_expected_jobs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage_skip_slots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage_publish_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage_advance_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("side_tag_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.ForeignKeyConstraint(["triggered_by_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["side_tag_id"], ["side_tags.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "build_chain_publish_receipts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("build_chain_id", sa.Integer(), nullable=False),
        sa.Column("build_attempt_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["build_chain_id"], ["build_chains.id"]),
        sa.ForeignKeyConstraint(["build_attempt_id"], ["build_attempts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("build_attempt_id"),
    )

    op.add_column("build_jobs", sa.Column("chain_id", sa.Integer(), nullable=True))
    op.add_column("build_jobs", sa.Column("chain_stage_index", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_build_jobs_chain_id", "build_jobs", "build_chains", ["chain_id"], ["id"])
    op.create_index("ix_build_jobs_chain_id", "build_jobs", ["chain_id"])
    op.execute(
        "UPDATE build_jobs SET chain_id = layer_plan_id, chain_stage_index = layer_index "
        "WHERE layer_plan_id IS NOT NULL"
    )

    op.drop_index("ix_layer_job_blockers_source_job", table_name="layer_job_blockers")
    op.drop_index("ix_layer_job_blockers_blocking_source", table_name="layer_job_blockers")
    op.drop_index("ix_layer_job_blockers_build_job_id", table_name="layer_job_blockers")
    op.drop_table("layer_job_blockers")

    op.drop_constraint("fk_build_jobs_layer_plan_id", "build_jobs", type_="foreignkey")
    op.drop_index("ix_build_jobs_layer_plan_id", table_name="build_jobs")
    op.drop_column("build_jobs", "layer_plan_id")
    op.drop_column("build_jobs", "layer_index")
    op.drop_column("build_jobs", "blocked_reason")

    op.drop_index("ix_layer_plans_side_tag_id", table_name="layer_plans")
    op.drop_index("ix_layer_plans_status", table_name="layer_plans")
    op.drop_index("ix_layer_plans_release_id", table_name="layer_plans")
    op.drop_table("layer_plans")
    sa.Enum(name="layerplanstatus").drop(op.get_bind(), checkfirst=True)
