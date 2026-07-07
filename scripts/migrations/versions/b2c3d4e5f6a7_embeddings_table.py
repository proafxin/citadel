"""embeddings table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-07 00:00:00.000000

"""
from typing import Sequence, Union

import pgvector.sqlalchemy
import sqlalchemy as sa

from alembic import op

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'embeddings',
        sa.Column('content_id', sa.String(), nullable=False),
        sa.Column('library_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('embedding', pgvector.sqlalchemy.HALFVEC(dim=1024), nullable=False),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['content_id'], ['content.content_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('content_id'),
    )
    op.create_index('ix_embeddings_library_id', 'embeddings', ['library_id'], unique=False)
    op.create_index(
        'ix_embeddings_embedding_hnsw',
        'embeddings',
        ['embedding'],
        unique=False,
        postgresql_using='hnsw',
        postgresql_ops={'embedding': 'halfvec_cosine_ops'},
    )
    op.drop_index('ix_content_embedding_hnsw', table_name='content')
    op.drop_column('content', 'embedding')


def downgrade() -> None:
    op.add_column('content', sa.Column('embedding', pgvector.sqlalchemy.Vector(dim=1024), nullable=True))
    op.create_index(
        'ix_content_embedding_hnsw',
        'content',
        ['embedding'],
        unique=False,
        postgresql_using='hnsw',
        postgresql_ops={'embedding': 'vector_cosine_ops'},
    )
    op.drop_index('ix_embeddings_embedding_hnsw', table_name='embeddings')
    op.drop_index('ix_embeddings_library_id', table_name='embeddings')
    op.drop_table('embeddings')
