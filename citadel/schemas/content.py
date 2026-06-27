from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class DocKind(StrEnum):
    PAGED = "paged"
    WORKBOOK = "workbook"


class DocStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class BlockType(StrEnum):
    TEXT = "text"
    TITLE = "title"
    LIST = "list"
    EQUATION = "equation"
    FIGURE = "figure"
    TABLE = "table"


class BBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class Block(BaseModel):
    page_idx: int
    type: BlockType
    bbox: BBox | None = None
    text: str | None = None
    text_level: int | None = None
    latex: str | None = None
    list_items: list[str] | None = None
    caption: str | None = None
    table_id: str | None = None


class Sheet(BaseModel):
    name: str
    index: int
    table_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DocumentBase(BaseModel):
    doc_id: int
    filename: str
    mime: str | None = None
    status: DocStatus = DocStatus.PENDING


class PagedDocument(DocumentBase):
    kind: Literal[DocKind.PAGED] = DocKind.PAGED
    blocks: list[Block] = Field(default_factory=list)


class WorkbookDocument(DocumentBase):
    kind: Literal[DocKind.WORKBOOK] = DocKind.WORKBOOK
    sheets: list[Sheet] = Field(default_factory=list)


Document = Annotated[PagedDocument | WorkbookDocument, Field(discriminator="kind")]
