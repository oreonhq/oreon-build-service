
from alembic import op
import sqlalchemy as sa

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releases",
        sa.Column(
            "default_repo_branch",
            sa.String(length=32),
            nullable=False,
            server_default="dev",
        ),
    )
    op.execute(
        """
        UPDATE build_jobs SET branch = 'dev'
        WHERE branch IS NOT NULL
          AND branch NOT IN ('dev', 'testing', 'stable')
        """
    )


def downgrade() -> None:
    op.drop_column("releases", "default_repo_branch")
