from sqlalchemy.orm import Mapped, mapped_column

from citadel.models.base import Base


class Library(Base):
    __tablename__ = "libraries"

    name: Mapped[str]
    tier: Mapped[str] = mapped_column(default="tier_1", server_default="tier_1")
