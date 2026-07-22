from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from citadel.models.base import Base

EMBED_DIM = 1024


class ContentNode(Base):
    __tablename__ = "content"

    # trigram GIN backs the sparse channel's ILIKE; declared here so autogenerate keeps it (see Embedding)
    __table_args__ = (
        Index(
            "ix_content_search_text_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        Index("ix_content_document_block", "document_id", "block_ordinal", unique=True),
    )

    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int]
    block_ordinal: Mapped[int | None] = mapped_column(default=None)
    page_no: Mapped[int | None] = mapped_column(default=None)
    sheet_no: Mapped[int | None] = mapped_column(default=None)
    type: Mapped[str]
    heading: Mapped[str | None] = mapped_column(default=None)
    bbox: Mapped[list[float] | None] = mapped_column(JSONB, default=None)
    raw: Mapped[dict | None] = mapped_column(JSONB, default=None)
    search_text: Mapped[str | None] = mapped_column(Text, default=None)
    token_count: Mapped[int | None] = mapped_column(default=None)  # BGE-M3 tokens in search_text, set at ingestion
    qwen_token_count: Mapped[int | None] = mapped_column(default=None)
