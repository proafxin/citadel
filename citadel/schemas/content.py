from pydantic import BaseModel


class Block(BaseModel):
    page_idx: int
    type: str
    text: str | None = None
    text_level: int | None = None
    list_items: list[str] | None = None
    bbox: list[float] | None = None
    font_size: float | None = None
