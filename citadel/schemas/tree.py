from pydantic import BaseModel


class ContentBlock(BaseModel):
    ordinal: int
    page_no: int
    type: str
    kind: str
    heading: str | None
    heading_path: list[str]
    bbox: list[float] | None
    text: str | None = None
    latex: str | None = None
    items: list[dict] | None = None
