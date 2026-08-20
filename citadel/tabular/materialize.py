import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal

from citadel.schemas.table import CellValue, Column, ColumnDType, TableStructure

logger = logging.getLogger(__name__)

SAMPLE_TABLE_ROWS = 10

_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+\.\d+")
_EXP = re.compile(r"-?\d+(\.\d+)?[eE][-+]?\d+")
_BOOLEAN_VALUES = {"True", "False"}
_MIDNIGHT = time(0, 0)


@dataclass
class MaterializedTable:
    sheet_no: int
    columns: list[Column]
    rows: list[list[CellValue]]
    sample_rows: list[list[CellValue]]
    n_rows: int
    title: str | None
    caption: str | None
    notes: list[str]
    anchors: dict
    formulas: list[str] | None = None
    header_rows: list[int] = field(default_factory=list)


def _lossless_int(value: str) -> bool:
    if not _INT.fullmatch(value):
        return False
    number = int(value)
    return str(number) == value and -(2**63) <= number <= 2**63 - 1


def _lossless_decimal(value: str) -> bool:
    return bool(_FLOAT.fullmatch(value)) and str(Decimal(value)) == value


def _lossless_float(value: str) -> bool:
    return bool(_EXP.fullmatch(value))


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _numeric_dtype(present: list[str]) -> ColumnDType | None:
    if all(_lossless_int(value) for value in present):
        return ColumnDType.INTEGER
    if all(_lossless_int(value) or _lossless_decimal(value) for value in present):
        return ColumnDType.DECIMAL
    if all(_lossless_int(value) or _lossless_decimal(value) or _lossless_float(value) for value in present):
        return ColumnDType.FLOAT
    return None


def _temporal_dtype(present: list[str]) -> ColumnDType | None:
    parsed = [dt for value in present if (dt := _parse_datetime(value)) is not None]
    if len(parsed) != len(present):
        return None
    return ColumnDType.DATE if all(dt.time() == _MIDNIGHT for dt in parsed) else ColumnDType.DATETIME


def dtype_of(values: list[str]) -> ColumnDType:
    present = [value for value in values if value]
    if not present:
        return ColumnDType.STRING
    if (numeric := _numeric_dtype(present)) is not None:
        return numeric
    if all(value in _BOOLEAN_VALUES for value in present):
        return ColumnDType.BOOLEAN
    if (temporal := _temporal_dtype(present)) is not None:
        return temporal
    return ColumnDType.STRING


def cast_cell(value: str) -> CellValue:
    return value or None


def _grid_cell(grid: list[list[str]], row: int, col: int) -> str:
    return grid[row][col] if 0 <= row < len(grid) and 0 <= col < len(grid[row]) else ""


def transpose_grid(grid: list[list[str]]) -> list[list[str]]:
    width = max((len(row) for row in grid), default=0)
    return [[_grid_cell(grid, row, col) for row in range(len(grid))] for col in range(width)]


def _grid_header(grid: list[list[str]], header_rows: list[int], col: int) -> str | None:
    parts = dict.fromkeys(cell for row in header_rows if (cell := _grid_cell(grid, row, col)))
    return " ".join(parts) or None


def _strip_notes(text: str | None, notes: list[str] | None) -> str | None:
    if text is None or not notes:
        return text
    stripped = text
    for note in notes:
        stripped = stripped.replace(note, "").strip()
    return stripped or None


def _naming_rows(grid: list[list[str]], header_rows: list[int], col_start: int, count: int) -> list[int]:
    if len(header_rows) <= 1:
        return header_rows
    naming = [
        row for row in header_rows if sum(1 for offset in range(count) if _grid_cell(grid, row, col_start + offset)) > 1
    ]
    return naming or header_rows


def _clean_name(name: str | None) -> str | None:
    return None if name is not None and _lossless_decimal(name.strip()) else name


def sample_rows(rows: list[list[CellValue]]) -> list[list[CellValue]]:
    if len(rows) <= SAMPLE_TABLE_ROWS:
        return list(rows)
    step = len(rows) / SAMPLE_TABLE_ROWS
    return [rows[int(index * step)] for index in range(SAMPLE_TABLE_ROWS)]


def _collect_rows(grid: list[list[str]], data_start: int, data_end: int, col_start: int, count: int) -> list[list[str]]:
    collected: list[list[str]] = []
    for offset in range(data_start, data_end + 1):
        raw = [_grid_cell(grid, offset, col_start + index) for index in range(count)]
        if any(raw):
            collected.append(raw)
    return collected


def _materialize_relational(
    grid: list[list[str]],
    structure: TableStructure,
    sheet_no: int,
    formulas: list[str] | None,
    extra_notes: list[str] | None,
    anchors: dict | None,
) -> MaterializedTable:
    count = structure.col_end - structure.col_start + 1
    header_rows = structure.header_rows or []
    collected = _collect_rows(grid, structure.data_start, structure.data_end, structure.col_start, count)
    dtypes = [dtype_of([raw[index] for raw in collected]) for index in range(count)]
    if header_rows:
        naming_rows = _naming_rows(grid, header_rows, structure.col_start, count)
        headers: list[str | None] = [
            _grid_header(grid, naming_rows, structure.col_start + index) for index in range(count)
        ]
    elif structure.columns:
        headers = [structure.columns[index] if index < len(structure.columns) else None for index in range(count)]
    else:
        headers = [None] * count
    headers = [_strip_notes(header, structure.notes) for header in headers]
    columns = [
        Column(header=_clean_name(headers[index]) or f"col{index}", dtype=dtypes[index]) for index in range(count)
    ]
    header_cells = [
        [cast_cell(_grid_cell(grid, row, structure.col_start + index)) for index in range(count)]
        for row in sorted(header_rows)
    ]
    data_rows = [
        [cast_cell(collected[position][index]) for index in range(count)] for position in range(len(collected))
    ]
    header_indices = list(range(len(header_cells)))
    return MaterializedTable(
        sheet_no=sheet_no,
        columns=columns,
        rows=[*header_cells, *data_rows],
        sample_rows=sample_rows(data_rows),
        n_rows=len(data_rows),
        title=structure.title,
        caption=structure.caption,
        notes=[*(structure.notes or []), *(extra_notes or [])],
        anchors={**(anchors or {}), "header_rows": header_indices},
        formulas=formulas,
        header_rows=header_indices,
    )


def _materialize_transposed(
    grid: list[list[str]],
    structure: TableStructure,
    sheet_no: int,
    formulas: list[str] | None,
    extra_notes: list[str] | None,
    anchors: dict | None,
) -> MaterializedTable:
    span = [
        [_grid_cell(grid, row, col) for col in range(structure.col_start, structure.col_end + 1)]
        for row in range(structure.data_start, structure.data_end + 1)
    ]
    flipped = transpose_grid(span)
    height = len(flipped)
    turned = TableStructure(
        col_start=0,
        col_end=max((len(row) for row in flipped), default=1) - 1,
        header_rows=[0] if height else [],
        data_start=1,
        data_end=height - 1,
        title=structure.title,
        caption=structure.caption,
        notes=structure.notes,
    )
    return _materialize_relational(flipped, turned, sheet_no, formulas, extra_notes, anchors)


def materialize(
    grid: list[list[str]],
    structure: TableStructure,
    *,
    sheet_no: int = 0,
    formulas: list[str] | None = None,
    extra_notes: list[str] | None = None,
    anchors: dict | None = None,
) -> MaterializedTable:
    if structure.transposed:
        return _materialize_transposed(grid, structure, sheet_no, formulas, extra_notes, anchors)
    return _materialize_relational(grid, structure, sheet_no, formulas, extra_notes, anchors)
