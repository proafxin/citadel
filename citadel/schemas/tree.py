from pydantic import BaseModel


class HeadingInfo(BaseModel):
    text: str
    font_size: float | None = None
    page: int
    context: str = ""


class NodeSpec(BaseModel):
    content_id: str
    parent_content_id: str | None
    ordinal: int
    page_no: int
    type: str
    kind: str
    level: int | None
    bbox: list[float] | None
    text: str | None = None
    latex: str | None = None
    items: list[dict] | None = None
    table_html: str | None = None
