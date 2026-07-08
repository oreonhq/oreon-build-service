
from alembic import op
import sqlalchemy as sa

revision = "032_default_distgit_branch"
down_revision = "033_rpmbuild_plan_followup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releases",
        sa.Column(
            "default_distgit_branch",
            sa.String(length=128),
            nullable=False,
            server_default="main",
        ),
    )


def downgrade() -> None:
    op.drop_column("releases", "default_distgit_branch")
