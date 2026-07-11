"""restore search indexes (hnsw, trgm gin, documents.library_id)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-11 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
    op.create_index(
        'ix_documents_library_id', 'documents', ['library_id'], unique=False, if_not_exists=True
    )
    op.create_index(
        'ix_content_search_text_trgm',
        'content',
        ['search_text'],
        unique=False,
        postgresql_using='gin',
        postgresql_ops={'search_text': 'gin_trgm_ops'},
        if_not_exists=True,
    )
    op.create_index(
        'ix_embeddings_embedding_hnsw',
        'embeddings',
        ['embedding'],
        unique=False,
        postgresql_using='hnsw',
        postgresql_ops={'embedding': 'halfvec_cosine_ops'},
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index('ix_embeddings_embedding_hnsw', table_name='embeddings')
    op.drop_index('ix_content_search_text_trgm', table_name='content')
    op.drop_index('ix_documents_library_id', table_name='documents')
