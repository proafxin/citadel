from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from citadel.models.base import Base
from citadel.models.content import EMBED_DIM


class Embedding(Base):
    __tablename__ = "embeddings"

    __table_args__ = (
        Index(
            "ix_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
    )

    content_id: Mapped[int] = mapped_column(ForeignKey("content.id", ondelete="CASCADE"), unique=True)
    library_id: Mapped[int] = mapped_column(index=True)
    type: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(HALFVEC(EMBED_DIM))
