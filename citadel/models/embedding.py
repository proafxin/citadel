from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from citadel.models.base import Base
from citadel.models.content import EMBED_DIM


class Embedding(Base):
    __tablename__ = "embeddings"

    content_id: Mapped[str] = mapped_column(ForeignKey("content.content_id", ondelete="CASCADE"), unique=True)
    library_id: Mapped[int] = mapped_column(index=True)
    type: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(HALFVEC(EMBED_DIM))
