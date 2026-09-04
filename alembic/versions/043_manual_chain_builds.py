"""Persist manual chain build execution options."""

from alembic import op
import sqlalchemy as sa


revision = "043_manual_chain_builds"
down_revision = "042_build_job_bridge_phase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rpmbuild_plans",
        sa.Column("options_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )


def downgrade() -> None:
    op.drop_column("rpmbuild_plans", "options_json")
