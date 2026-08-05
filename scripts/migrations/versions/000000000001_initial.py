from typing import Sequence, Union

import pgvector.sqlalchemy
import sqlalchemy as sa

from alembic import op

revision: str = '000000000001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TIMESTAMPS = (
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
)


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')

    op.create_table(
        'libraries',
        *TIMESTAMPS,
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('tier', sa.String(), server_default='tier_1', nullable=False),
        sa.Column('status', sa.String(), server_default='ready', nullable=False),
        sa.Column('ingest_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finalize_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('described_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('embed_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ready_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'documents',
        *TIMESTAMPS,
        sa.Column('library_id', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('processing_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ingest_seconds', sa.Float(), nullable=True),
        sa.Column('blocks_in', sa.Integer(), nullable=True),
        sa.Column('nodes_out', sa.Integer(), nullable=True),
        sa.Column('drops', sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column('paratext', sa.dialects.postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(['library_id'], ['libraries.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_documents_library_id', 'documents', ['library_id'])

    op.create_table(
        'content',
        *TIMESTAMPS,
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('block_ordinal', sa.Integer(), nullable=True),
        sa.Column('page_no', sa.Integer(), nullable=True),
        sa.Column('sheet_no', sa.Integer(), nullable=True),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('heading', sa.String(), nullable=True),
        sa.Column('bbox', sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column('raw', sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column('search_text', sa.Text(), nullable=True),
        sa.Column('token_count', sa.Integer(), nullable=True),
        sa.Column('qwen_token_count', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_content_document_id', 'content', ['document_id'])
    op.create_index('ix_content_document_block', 'content', ['document_id', 'block_ordinal'], unique=True)
    op.create_index(
        'ix_content_search_text_trgm',
        'content',
        ['search_text'],
        postgresql_using='gin',
        postgresql_ops={'search_text': 'gin_trgm_ops'},
    )

    op.create_table(
        'content_batches',
        *TIMESTAMPS,
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('batch_no', sa.Integer(), nullable=False),
        sa.Column('start_block_ordinal', sa.Integer(), nullable=False),
        sa.Column('end_block_ordinal', sa.Integer(), nullable=False),
        sa.Column('start_page_no', sa.Integer(), nullable=True),
        sa.Column('end_page_no', sa.Integer(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('summary_tokens', sa.Integer(), nullable=False),
        sa.Column('content_tokens', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_content_batches_document_id', 'content_batches', ['document_id'])
    op.create_index('ix_content_batches_document_batch', 'content_batches', ['document_id', 'batch_no'], unique=True)

    op.create_table(
        'tables',
        *TIMESTAMPS,
        sa.Column('content_id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('columns', sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column('table_metadata', sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column('n_rows', sa.Integer(), nullable=False),
        sa.Column('sample_rows', sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column('anchors', sa.dialects.postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(['content_id'], ['content.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('content_id'),
    )
    op.create_index('ix_tables_document_id', 'tables', ['document_id'])

    op.create_table(
        'table_rows',
        *TIMESTAMPS,
        sa.Column('table_id', sa.Integer(), nullable=False),
        sa.Column('row_idx', sa.Integer(), nullable=False),
        sa.Column('values', sa.dialects.postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(['table_id'], ['tables.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_table_rows_table_id', 'table_rows', ['table_id'])

    op.create_table(
        'embeddings',
        *TIMESTAMPS,
        sa.Column('content_id', sa.Integer(), nullable=False),
        sa.Column('library_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('embedding', pgvector.sqlalchemy.HALFVEC(1024), nullable=False),
        sa.ForeignKeyConstraint(['content_id'], ['content.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('content_id'),
    )
    op.create_index('ix_embeddings_library_id', 'embeddings', ['library_id'])
    op.create_index(
        'ix_embeddings_embedding_hnsw',
        'embeddings',
        ['embedding'],
        postgresql_using='hnsw',
        postgresql_ops={'embedding': 'halfvec_cosine_ops'},
    )


def downgrade() -> None:
    op.drop_table('embeddings')
    op.drop_table('table_rows')
    op.drop_table('tables')
    op.drop_table('content_batches')
    op.drop_table('content')
    op.drop_table('documents')
    op.drop_table('libraries')
