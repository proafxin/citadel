from sqlalchemy.orm import Mapped

from citadel.models.base import Base


class Library(Base):
    __tablename__ = "libraries"

    name: Mapped[str]
