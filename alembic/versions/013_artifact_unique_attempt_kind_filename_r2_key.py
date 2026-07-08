
from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY build_attempt_id, kind, filename, r2_key
                    ORDER BY id
                ) AS rn
            FROM artifacts
        )
        DELETE FROM artifacts
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )

    op.create_unique_constraint(
        "ux_artifacts_attempt_kind_filename_r2_key",
        "artifacts",
        ["build_attempt_id", "kind", "filename", "r2_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "ux_artifacts_attempt_kind_filename_r2_key",
        "artifacts",
        type_="unique",
    )

