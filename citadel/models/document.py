from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from citadel.models.base import Base
from citadel.models.status import DocumentStatus


class Document(Base):
    __tablename__ = "documents"

    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str]
    title: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(default=DocumentStatus.QUEUED)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    ingest_seconds: Mapped[float | None] = mapped_column(default=None)
    blocks_in: Mapped[int | None] = mapped_column(default=None)
    nodes_out: Mapped[int | None] = mapped_column(default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)  # what this document covers, reduced from its
    # own batch summaries once they all exist. it is the document's entry in the inventory the query is resolved
    # against, so it is written to a ceiling: the whole library's entries must fit ONE resolution call
    summarized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)  # when this
    # document's batches were packed AND summarized. a document with no batches at all is still marked, so "not yet
    # summarized" and "summarized, nothing to summarize" are distinguishable — which is what the library waits on
    drops: Mapped[dict | None] = mapped_column(JSONB, default=None)
    paratext: Mapped[list | None] = mapped_column(JSONB, default=None)
