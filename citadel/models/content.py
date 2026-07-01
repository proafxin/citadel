from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from citadel.models.base import Base

EMBED_DIM = 1024


class ContentNode(Base):
    __tablename__ = "content"

    content_id: Mapped[str] = mapped_column(unique=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("content.id"), index=True, default=None)
    ordinal: Mapped[int]
    page_no: Mapped[int | None] = mapped_column(default=None)
    sheet_no: Mapped[int | None] = mapped_column(default=None)
    type: Mapped[str]
    level: Mapped[int | None] = mapped_column(default=None)
    label: Mapped[str | None] = mapped_column(default=None)
    bbox: Mapped[list[float] | None] = mapped_column(JSONB, default=None)
    search_text: Mapped[str | None] = mapped_column(Text, default=None)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), default=None)


class Paragraph(Base):
    __tablename__ = "paragraphs"

    content_id: Mapped[str] = mapped_column(ForeignKey("content.content_id"), unique=True)
    text: Mapped[str] = mapped_column(Text)


class Code(Base):
    __tablename__ = "codes"

    content_id: Mapped[str] = mapped_column(ForeignKey("content.content_id"), unique=True)
    text: Mapped[str] = mapped_column(Text)


class Equation(Base):
    __tablename__ = "equations"

    content_id: Mapped[str] = mapped_column(ForeignKey("content.content_id"), unique=True)
    latex: Mapped[str] = mapped_column(Text)


class ListBlock(Base):
    __tablename__ = "lists"

    content_id: Mapped[str] = mapped_column(ForeignKey("content.content_id"), unique=True)
    items: Mapped[list[dict]] = mapped_column(JSONB)
