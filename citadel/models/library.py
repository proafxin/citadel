from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from citadel.models.base import Base
from citadel.models.status import LibraryStatus


class Library(Base):
    __tablename__ = "libraries"

    name: Mapped[str]
    tier: Mapped[str] = mapped_column(default="tier_1", server_default="tier_1")
    status: Mapped[str] = mapped_column(default=LibraryStatus.READY, server_default=LibraryStatus.READY)
    ingest_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    embed_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
