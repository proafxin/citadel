from enum import StrEnum

from pydantic import BaseModel

type CellValue = str | int | float | bool | None


class ColumnDType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"


class Column(BaseModel):
    header: str | None = None
    dtype: ColumnDType = ColumnDType.STRING


class TableStructure(BaseModel):
    transposed: bool = False
    col_start: int
    col_end: int
    header_rows: list[int] | None = None
    data_start: int
    data_end: int
    columns: list[str] | None = None
    section_rows: list[int] | None = None
    title: str | None = None
    caption: str | None = None
    notes: list[str] | None = None
