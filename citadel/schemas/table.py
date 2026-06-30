from enum import StrEnum

from pydantic import BaseModel, Field

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
