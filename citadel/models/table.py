from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from citadel.models.base import Base


class Table(Base):
    __tablename__ = "tables"

    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    key: Mapped[str] = mapped_column(unique=True)
    sheet_no: Mapped[int | None] = mapped_column(default=None)
    page_no: Mapped[int | None] = mapped_column(default=None)
    ordinal: Mapped[int]
    page_range: Mapped[list[int] | None] = mapped_column(JSONB, default=None)
    columns: Mapped[list[dict]] = mapped_column(JSONB)
    table_metadata: Mapped[dict] = mapped_column(JSONB)
    description: Mapped[str] = mapped_column(Text)
    n_rows: Mapped[int]
    sample_rows: Mapped[list[list]] = mapped_column(JSONB)


class TableRow(Base):
    __tablename__ = "table_rows"

    table_id: Mapped[int] = mapped_column(ForeignKey("tables.id"), index=True)
    row_idx: Mapped[int]
    values: Mapped[list] = mapped_column(JSONB)
