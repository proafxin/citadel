from typing import Sequence, Union

from alembic import op

revision: str = 'b1c4e2f9a730'
down_revision: Union[str, Sequence[str], None] = 'a8d720c477fb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('content', 'qwen_token_count', new_column_name='token_count')


def downgrade() -> None:
    op.alter_column('content', 'token_count', new_column_name='qwen_token_count')
