"""initial migration

Revision ID: ad0ae9421017
Revises:
Create Date: 2026-06-28 17:32:18.560046

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'ad0ae9421017'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('libraries',
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('documents',
    sa.Column('library_id', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['library_id'], ['libraries.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('content',
    sa.Column('content_id', sa.String(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('page_no', sa.Integer(), nullable=True),
    sa.Column('sheet_no', sa.Integer(), nullable=True),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('level', sa.Integer(), nullable=True),
    sa.Column('label', sa.String(), nullable=True),
    sa.Column('bbox', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('search_text', sa.Text(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ),
    sa.ForeignKeyConstraint(['parent_id'], ['content.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('content_id')
    )
    op.create_index(op.f('ix_content_document_id'), 'content', ['document_id'], unique=False)
    op.create_index(op.f('ix_content_parent_id'), 'content', ['parent_id'], unique=False)
    op.create_table('codes',
    sa.Column('content_id', sa.String(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['content_id'], ['content.content_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('content_id')
    )
    op.create_table('equations',
    sa.Column('content_id', sa.String(), nullable=False),
    sa.Column('latex', sa.Text(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['content_id'], ['content.content_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('content_id')
    )
    op.create_table('lists',
    sa.Column('content_id', sa.String(), nullable=False),
    sa.Column('items', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['content_id'], ['content.content_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('content_id')
    )
    op.create_table('paragraphs',
    sa.Column('content_id', sa.String(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['content_id'], ['content.content_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('content_id')
    )
    op.create_table('tables',
    sa.Column('content_id', sa.String(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('columns', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('table_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('n_rows', sa.Integer(), nullable=False),
    sa.Column('sample_rows', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('anchors', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['content_id'], ['content.content_id'], ),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('content_id')
    )
    op.create_index(op.f('ix_tables_document_id'), 'tables', ['document_id'], unique=False)
    op.create_table('table_rows',
    sa.Column('table_id', sa.Integer(), nullable=False),
    sa.Column('row_idx', sa.Integer(), nullable=False),
    sa.Column('values', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['table_id'], ['tables.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_table_rows_table_id'), 'table_rows', ['table_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_table_rows_table_id'), table_name='table_rows')
    op.drop_table('table_rows')
    op.drop_index(op.f('ix_tables_document_id'), table_name='tables')
    op.drop_table('tables')
    op.drop_table('paragraphs')
    op.drop_table('lists')
    op.drop_table('equations')
    op.drop_table('codes')
    op.drop_index(op.f('ix_content_parent_id'), table_name='content')
    op.drop_index(op.f('ix_content_document_id'), table_name='content')
    op.drop_table('content')
    op.drop_table('documents')
    op.drop_table('libraries')
