from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from citadel.models.base import Base
from citadel.models.status import DocumentStatus


class Document(Base):
    __tablename__ = "documents"

    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    filename: Mapped[str]
    status: Mapped[str] = mapped_column(default=DocumentStatus.QUEUED)
    ingest_seconds: Mapped[float | None] = mapped_column(default=None)
