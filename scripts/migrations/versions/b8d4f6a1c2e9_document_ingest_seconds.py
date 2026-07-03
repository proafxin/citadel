from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4f6a1c2e9"
down_revision: str | Sequence[str] | None = "a7c9e1b2d3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("ingest_seconds", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "ingest_seconds")
