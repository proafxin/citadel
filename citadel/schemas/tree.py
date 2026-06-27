from pydantic import BaseModel, Field

from citadel.schemas.table import CellValue, Column

JsonValue = dict | list | str | int | float | bool | None


class TableView(BaseModel):
    columns: list[Column]
    rows: list[list[CellValue]]
    description: str
    caption: str | None = None


class TreeNode(BaseModel):
    type: str
    level: int | None = None
    label: str | None = None
    content: str | None = None
    list_items: list[dict] | None = None
    json: JsonValue = None
    table: TableView | None = None
    children: list["TreeNode"] = Field(default_factory=list)


class DocumentTree(BaseModel):
    filename: str
    nodes: list[TreeNode] = Field(default_factory=list)


class LibraryTree(BaseModel):
    name: str
    documents: list[DocumentTree] = Field(default_factory=list)
