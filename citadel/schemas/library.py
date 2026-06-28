from pydantic import BaseModel


class LibraryCreate(BaseModel):
    name: str


class LibraryRead(BaseModel):
    id: int
    name: str
