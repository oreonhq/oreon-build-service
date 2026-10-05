"""ISO builds queued for workers."""

from alembic import op
import sqlalchemy as sa


revision = "044_iso_builds"
down_revision = "043_manual_chain_builds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "iso_builds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("architecture", sa.String(32), nullable=False),
        sa.Column("mock_environment_id", sa.Integer(), sa.ForeignKey("mock_environments.id"), nullable=False),
        sa.Column("mock_config", sa.String(128), nullable=False),
        sa.Column("release_folder", sa.String(128), nullable=False),
        sa.Column("kickstart", sa.String(256), nullable=False),
        sa.Column("iso_name", sa.String(256), nullable=False),
        sa.Column("project", sa.String(64), nullable=False),
        sa.Column("volid", sa.String(32), nullable=False),
        sa.Column("releasever", sa.String(16), nullable=False),
        sa.Column("macboot", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("r2_key", sa.String(256), nullable=True),
        sa.Column("log_text", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.Integer(), sa.ForeignKey("workers.id"), nullable=True),
        sa.Column("triggered_by_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_iso_builds_status", "iso_builds", ["status"])
    op.create_index("ix_iso_builds_architecture", "iso_builds", ["architecture"])


def downgrade() -> None:
    op.drop_index("ix_iso_builds_architecture", table_name="iso_builds")
    op.drop_index("ix_iso_builds_status", table_name="iso_builds")
    op.drop_table("iso_builds")
