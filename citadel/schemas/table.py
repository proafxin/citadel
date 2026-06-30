from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

type CellValue = str | int | float | bool | None


class ColumnDType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"


class ColumnRole(StrEnum):
    DATA = "data"
    SECTION = "section"


class Column(BaseModel):
    header: str | None = None
    dtype: ColumnDType = ColumnDType.STRING
    role: ColumnRole = ColumnRole.DATA
    unit: str | None = None


class TableOrigin(BaseModel):
    ordinal: int
    sheet_no: int | None = None
    page_no: int | None = None
    page_range: tuple[int, int] | None = None


class TableMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: str | None = None
    caption: str | None = None
    notes: list[str] = Field(default_factory=list)
    has_merged_cells: bool = False
    header_levels: int = 1


class Table(BaseModel):
    content_id: str
    doc_id: int
    origin: TableOrigin
    columns: list[Column]
    n_rows: int
    sample_rows: list[list[CellValue]]
    description: str
    metadata: TableMetadata = Field(default_factory=TableMetadata)
    anchors: list[dict] | None = None


class TableRow(BaseModel):
    content_id: str
    row_idx: int
    values: list[CellValue]


class StructureColumn(BaseModel):
    header: str | None = None
    dtype: ColumnDType = ColumnDType.STRING
    role: ColumnRole = ColumnRole.DATA
    unit: str | None = None


class TableStructure(BaseModel):
    col_start: int
    col_end: int
    header_rows: list[int] = Field(default_factory=list)
    data_start: int
    data_end: int
    columns: list[StructureColumn]
    section_label_col: int | None = None
    title: str | None = None
    caption: str | None = None
    notes: list[str] = Field(default_factory=list)
    description: str = ""


class RegionStructure(BaseModel):
    tables: list[TableStructure]
