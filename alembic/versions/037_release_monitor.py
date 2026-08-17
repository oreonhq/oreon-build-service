
from alembic import op
import sqlalchemy as sa

revision = "037_release_monitor"
down_revision = "036_layer_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "release_monitor_scans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=True),
        sa.Column("branches", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("total_packages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checked_packages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_packages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outdated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_account_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.ForeignKeyConstraint(["created_by_account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_release_monitor_scans_release_id", "release_monitor_scans", ["release_id"])
    op.create_index("ix_release_monitor_scans_status", "release_monitor_scans", ["status"])

    op.create_table(
        "release_monitor_outdated",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scan_id", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=True),
        sa.Column("package_name", sa.String(length=256), nullable=False),
        sa.Column("current_version", sa.String(length=256), nullable=False),
        sa.Column("latest_version", sa.String(length=256), nullable=False),
        sa.Column("project", sa.String(length=256), nullable=True),
        sa.Column("ecosystem", sa.String(length=512), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["scan_id"], ["release_monitor_scans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_release_monitor_outdated_scan_id", "release_monitor_outdated", ["scan_id"])
    op.create_index("ix_release_monitor_outdated_release_id", "release_monitor_outdated", ["release_id"])
    op.create_index("ix_release_monitor_outdated_package_name", "release_monitor_outdated", ["package_name"])


def downgrade() -> None:
    op.drop_index("ix_release_monitor_outdated_package_name", table_name="release_monitor_outdated")
    op.drop_index("ix_release_monitor_outdated_release_id", table_name="release_monitor_outdated")
    op.drop_index("ix_release_monitor_outdated_scan_id", table_name="release_monitor_outdated")
    op.drop_table("release_monitor_outdated")

    op.drop_index("ix_release_monitor_scans_status", table_name="release_monitor_scans")
    op.drop_index("ix_release_monitor_scans_release_id", table_name="release_monitor_scans")
    op.drop_table("release_monitor_scans")
