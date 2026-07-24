"""document summarized_at

Revision ID: 000000000002
Revises: 000000000001
Create Date: 2026-07-24 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = '000000000002'
down_revision: Union[str, Sequence[str], None] = '000000000001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('summarized_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('documents', 'summarized_at')
