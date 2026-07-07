"""retrieval indexes

Revision ID: a1b2c3d4e5f6
Revises: 58a3f55c378a
Create Date: 2026-07-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '58a3f55c378a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
    op.create_index('ix_documents_library_id', 'documents', ['library_id'], unique=False)
    op.create_index(
        'ix_content_search_text_trgm',
        'content',
        ['search_text'],
        unique=False,
        postgresql_using='gin',
        postgresql_ops={'search_text': 'gin_trgm_ops'},
    )
    op.create_index(
        'ix_content_embedding_hnsw',
        'content',
        ['embedding'],
        unique=False,
        postgresql_using='hnsw',
        postgresql_ops={'embedding': 'vector_cosine_ops'},
    )


def downgrade() -> None:
    op.drop_index('ix_content_embedding_hnsw', table_name='content')
    op.drop_index('ix_content_search_text_trgm', table_name='content')
    op.drop_index('ix_documents_library_id', table_name='documents')
