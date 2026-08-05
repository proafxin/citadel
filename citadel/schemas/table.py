from enum import StrEnum
from typing import Literal

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


class KeyColumn(BaseModel):
    name: str
    col: int


class Dimension(BaseModel):
    name: str
    header_row: int


class Crosstab(BaseModel):
    key_columns: list[KeyColumn]
    dimensions: list[Dimension]
    value_name: str
    value_col_start: int
    value_col_end: int


class TableStructure(BaseModel):
    layout: Literal["relational", "crosstab"] = "relational"
    transposed: bool = False
    col_start: int
    col_end: int
    header_rows: list[int] | None = None
    data_start: int
    data_end: int
    columns: list[str] | None = None
    section_rows: list[int] | None = None
    crosstab: Crosstab | None = None
    title: str | None = None
    caption: str | None = None
    notes: list[str] | None = None
