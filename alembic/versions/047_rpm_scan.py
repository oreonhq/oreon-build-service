"""Scan state and findings before repo publish."""

from alembic import op
import sqlalchemy as sa


revision = "047_rpm_scan"
down_revision = "046_automaint_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("build_jobs", sa.Column("scan_state", sa.String(length=32), nullable=True))
    op.create_index("ix_build_jobs_scan_state", "build_jobs", ["scan_state"])
    op.create_table(
        "rpm_scan_findings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("build_job_id", sa.Integer(), sa.ForeignKey("build_jobs.id"), nullable=False),
        sa.Column("attempt_id", sa.Integer(), sa.ForeignKey("build_attempts.id"), nullable=True),
        sa.Column("engine", sa.String(length=16), nullable=False),
        sa.Column("rule", sa.String(length=256), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_rpm_scan_findings_build_job_id", "rpm_scan_findings", ["build_job_id"])
    op.create_index("ix_rpm_scan_findings_attempt_id", "rpm_scan_findings", ["attempt_id"])


def downgrade() -> None:
    op.drop_index("ix_rpm_scan_findings_attempt_id", table_name="rpm_scan_findings")
    op.drop_index("ix_rpm_scan_findings_build_job_id", table_name="rpm_scan_findings")
    op.drop_table("rpm_scan_findings")
    op.drop_index("ix_build_jobs_scan_state", table_name="build_jobs")
    op.drop_column("build_jobs", "scan_state")
