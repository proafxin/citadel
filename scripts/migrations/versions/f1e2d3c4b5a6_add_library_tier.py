from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1e2d3c4b5a6"
down_revision: str | Sequence[str] | None = "a47dfeb7f03a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("libraries", sa.Column("tier", sa.String(), nullable=False, server_default="tier_1"))


def downgrade() -> None:
    op.drop_column("libraries", "tier")
