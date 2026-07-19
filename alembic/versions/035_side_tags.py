
from alembic import op
import sqlalchemy as sa

revision = "035_side_tags"
down_revision = "034_build_job_options"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "side_tags",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("owner_account_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_from",
            sa.Enum("manual", "github_pr", "github_push", name="sidetagcreatedfrom", native_enum=False),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("release_id", "name", name="uq_side_tags_release_name"),
    )
    op.create_index("ix_side_tags_release_id", "side_tags", ["release_id"])
    op.create_index("ix_side_tags_owner_account_id", "side_tags", ["owner_account_id"])
    op.create_index("ix_side_tags_last_activity_at", "side_tags", ["last_activity_at"])

    op.create_table(
        "side_tag_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("side_tag_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["side_tag_id"], ["side_tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("side_tag_id", "account_id", name="uq_side_tag_members_tag_account"),
    )
    op.create_index("ix_side_tag_members_side_tag_id", "side_tag_members", ["side_tag_id"])
    op.create_index("ix_side_tag_members_account_id", "side_tag_members", ["account_id"])

    op.add_column("build_jobs", sa.Column("side_tag_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_build_jobs_side_tag_id",
        "build_jobs",
        "side_tags",
        ["side_tag_id"],
        ["id"],
    )
    op.create_index("ix_build_jobs_side_tag_id", "build_jobs", ["side_tag_id"])
    op.alter_column(
        "build_jobs",
        "branch",
        existing_type=sa.String(length=32),
        type_=sa.String(length=128),
        existing_nullable=False,
    )

    op.add_column("build_chains", sa.Column("side_tag_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_build_chains_side_tag_id",
        "build_chains",
        "side_tags",
        ["side_tag_id"],
        ["id"],
    )
    op.create_index("ix_build_chains_side_tag_id", "build_chains", ["side_tag_id"])


def downgrade() -> None:
    op.drop_index("ix_build_chains_side_tag_id", table_name="build_chains")
    op.drop_constraint("fk_build_chains_side_tag_id", "build_chains", type_="foreignkey")
    op.drop_column("build_chains", "side_tag_id")

    op.alter_column(
        "build_jobs",
        "branch",
        existing_type=sa.String(length=128),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.drop_index("ix_build_jobs_side_tag_id", table_name="build_jobs")
    op.drop_constraint("fk_build_jobs_side_tag_id", "build_jobs", type_="foreignkey")
    op.drop_column("build_jobs", "side_tag_id")

    op.drop_index("ix_side_tag_members_account_id", table_name="side_tag_members")
    op.drop_index("ix_side_tag_members_side_tag_id", table_name="side_tag_members")
    op.drop_table("side_tag_members")

    op.drop_index("ix_side_tags_last_activity_at", table_name="side_tags")
    op.drop_index("ix_side_tags_owner_account_id", table_name="side_tags")
    op.drop_index("ix_side_tags_release_id", table_name="side_tags")
    op.drop_table("side_tags")
