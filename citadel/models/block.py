from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from citadel.models.base import Base


class Block(Base):
    __tablename__ = "blocks"

    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    page_idx: Mapped[int]
    type: Mapped[str]
    text: Mapped[str | None] = mapped_column(Text, default=None)
    text_level: Mapped[int | None] = mapped_column(default=None)
    list_items: Mapped[list[str] | None] = mapped_column(JSONB, default=None)
    bbox: Mapped[list[float] | None] = mapped_column(JSONB, default=None)
