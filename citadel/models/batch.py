from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from citadel.models.base import Base


class ContentBatch(Base):
    __tablename__ = "content_batches"

    __table_args__ = (Index("ix_content_batches_document_batch", "document_id", "batch_no", unique=True),)

    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    batch_no: Mapped[int]
    start_block_ordinal: Mapped[int]
    end_block_ordinal: Mapped[int]
    start_page_no: Mapped[int | None] = mapped_column(default=None)
    end_page_no: Mapped[int | None] = mapped_column(default=None)
    summary: Mapped[str] = mapped_column(Text)
    summary_tokens: Mapped[int]
    content_tokens: Mapped[int]
