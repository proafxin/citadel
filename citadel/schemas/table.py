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
    col: int  # grid column index of a row-key column, carried through the unpivot unchanged


class Dimension(BaseModel):
    name: str
    header_row: int  # the header row whose cells label each value column with this dimension's value


class Crosstab(BaseModel):
    # a matrix: value_col_start..value_col_end hold one measure, labelled by the dimension header rows; the key columns
    # identify the row. unpivoting emits one output row per non-empty value cell: key values + dimension values + cell
    key_columns: list[KeyColumn]
    dimensions: list[Dimension]
    value_name: str
    value_col_start: int
    value_col_end: int


class TableStructure(BaseModel):
    layout: Literal["relational", "crosstab"] = "relational"
    col_start: int
    col_end: int
    header_rows: list[int] | None = None
    data_start: int
    data_end: int
    columns: list[str] | None = None  # SLM-resolved column names (relational); None → derive from header_rows
    crosstab: Crosstab | None = None
    title: str | None = None
    caption: str | None = None
    notes: list[str] | None = None


class RegionStructure(BaseModel):
    tables: list[TableStructure]
