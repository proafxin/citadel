import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision = "b2f1a9c4d7e3"
down_revision = "ad0ae9421017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.add_column("content", sa.Column("embedding", Vector(1024), nullable=True))
    op.execute("CREATE INDEX ix_content_embedding ON content USING hnsw (embedding vector_cosine_ops)")
    op.execute("CREATE INDEX ix_content_search_text_trgm ON content USING gin (search_text gin_trgm_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_content_search_text_trgm")
    op.execute("DROP INDEX IF EXISTS ix_content_embedding")
    op.drop_column("content", "embedding")
