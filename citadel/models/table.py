from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from citadel.models.base import Base


class Table(Base):
    __tablename__ = "tables"

    content_id: Mapped[str] = mapped_column(ForeignKey("content.content_id", ondelete="CASCADE"), unique=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    columns: Mapped[list[dict]] = mapped_column(JSONB)
    table_metadata: Mapped[dict] = mapped_column(JSONB)
    description: Mapped[str] = mapped_column(Text)
    n_rows: Mapped[int]
    sample_rows: Mapped[list[list]] = mapped_column(JSONB)
    anchors: Mapped[dict | None] = mapped_column(JSONB, default=None)


class TableRow(Base):
    __tablename__ = "table_rows"

    table_id: Mapped[int] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"), index=True)
    row_idx: Mapped[int]
    values: Mapped[list] = mapped_column(JSONB)
