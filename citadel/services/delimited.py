from datetime import date, datetime
from io import BytesIO

import polars as pl

from citadel.schemas.table import CellValue, Column, ColumnDType
from citadel.services.excel import SAMPLE_TABLE_ROWS, MaterializedTable

_DTYPE_BY_PREFIX = {
    "Int": ColumnDType.INTEGER,
    "UInt": ColumnDType.INTEGER,
    "Float": ColumnDType.FLOAT,
    "Decimal": ColumnDType.DECIMAL,
    "Boolean": ColumnDType.BOOLEAN,
    "Datetime": ColumnDType.DATETIME,
    "Date": ColumnDType.DATE,
}


def _map_dtype(dtype: pl.DataType) -> ColumnDType:
    name = str(dtype)
    for prefix, mapped in _DTYPE_BY_PREFIX.items():
        if name.startswith(prefix):
            return mapped
    return ColumnDType.STRING


def _cell(value: object) -> CellValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def read_csv_table(data: bytes, separator: str) -> MaterializedTable:
    frame = pl.read_csv(BytesIO(data), separator=separator, infer_schema_length=10000, truncate_ragged_lines=True)
    columns = [Column(header=name, dtype=_map_dtype(dtype)) for name, dtype in frame.schema.items()]
    rows = [[_cell(value) for value in row] for row in frame.iter_rows()]
    return MaterializedTable(
        sheet_no=0,
        columns=columns,
        rows=rows,
        sample_rows=rows[:SAMPLE_TABLE_ROWS],
        n_rows=len(rows),
        title=None,
        caption=None,
        notes=[],
        description="",
        anchors=None,
    )
