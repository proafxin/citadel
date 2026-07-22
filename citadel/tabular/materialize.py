import re
from dataclasses import dataclass, field
from decimal import Decimal

from citadel.schemas.table import CellValue, Column, ColumnDType, TableStructure

SAMPLE_TABLE_ROWS = 10

_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+\.\d+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class MaterializedTable:
    sheet_no: int
    columns: list[Column]
    rows: list[list[CellValue]]  # the FULL stored grid: header rows first (verbatim), then data rows. header rows are
    # kept, not deleted — a row the detector wrongly called a header (measured: Coca-Cola's NET OPERATING REVENUES, its
    # top P&L line) survives as a queryable row instead of vanishing into a column name. what is a header is recorded in
    # header_rows, not enforced by removal, so a wrong call is a mislabel over intact data, never a lost row
    sample_rows: list[list[CellValue]]  # DATA rows only — the sample and n_rows are the data view, header rows excluded
    n_rows: int
    title: str | None
    caption: str | None
    notes: list[str]
    anchors: dict
    formulas: list[str] | None = None
    header_rows: list[int] = field(default_factory=list)  # indices into `rows` that are header, not data. the query
    # projection skips exactly these, so the typed view is unchanged while the grid stays whole and reconstructable


def _lossless_int(value: str) -> bool:
    # a numeric type is assigned ONLY if the value round-trips back to its exact source text. rejects "007", "+5",
    # " 5 " etc. so codes/ids with leading zeros stay verbatim strings and are never silently renumbered. also bounded
    # to signed 64-bit so a long numeric id can't overflow the integer cast at query time — it stays a string instead
    if not _INT.fullmatch(value):
        return False
    number = int(value)
    return str(number) == value and -(2**63) <= number <= 2**63 - 1


def _lossless_decimal(value: str) -> bool:
    # Decimal preserves trailing zeros and exact digits ("1.50" stays "1.50"); leading-zero/exponent forms don't
    # round-trip and fall through to string
    return bool(_FLOAT.fullmatch(value)) and str(Decimal(value)) == value


def dtype_of(values: list[str]) -> ColumnDType:
    present = [value for value in values if value]
    if not present:
        return ColumnDType.STRING
    if all(_lossless_int(value) for value in present):
        return ColumnDType.INTEGER
    if all(_lossless_int(value) or _lossless_decimal(value) for value in present):
        return ColumnDType.DECIMAL
    return ColumnDType.STRING


def cast_cell(value: str) -> CellValue:
    # never converts: every cell is stored as its exact source text (empty → None). the dtype travels as a hint on the
    # column and the query casts on demand — so dirty cells, codes and ids are never silently altered or dropped
    return value or None


def _grid_cell(grid: list[list[str]], row: int, col: int) -> str:
    return grid[row][col] if 0 <= row < len(grid) and 0 <= col < len(grid[row]) else ""


def _grid_header(grid: list[list[str]], header_rows: list[int], col: int) -> str | None:
    parts = dict.fromkeys(cell for row in header_rows if (cell := _grid_cell(grid, row, col)))
    return " ".join(parts) or None


def _is_data_token(cell: str) -> bool:
    value = cell.strip()
    return bool(_EMAIL_RE.match(value) or _ISO_DATE_RE.match(value))


def _plausible_header_rows(grid: list[list[str]], header_rows: list[int]) -> list[int]:
    # a header NAMES the columns; it is never the data itself. emails and ISO dates are never column names, so a
    # predicted header row made mostly of them is a data row the model promoted (its cells then get space-joined into
    # "103 104" / two emails). bare numbers are deliberately NOT a data signal: wide sheets legitimately use years as
    # headers (Country Name | ... | 1960 | 1961 | ...), and rejecting those would destroy a correct schema.
    kept: list[int] = []
    for row in header_rows:
        cells = [cell for cell in (grid[row] if row < len(grid) else []) if cell.strip()]
        if cells and sum(1 for cell in cells if _is_data_token(cell)) * 2 > len(cells):
            continue
        kept.append(row)
    return kept


def _sample(rows: list[list[CellValue]]) -> list[list[CellValue]]:
    if len(rows) <= SAMPLE_TABLE_ROWS:
        return list(rows)
    step = len(rows) / SAMPLE_TABLE_ROWS
    return [rows[int(index * step)] for index in range(SAMPLE_TABLE_ROWS)]


def materialize(
    grid: list[list[str]],
    structure: TableStructure,
    *,
    sheet_no: int = 0,
    formulas: list[str] | None = None,
    extra_notes: list[str] | None = None,
    anchors: dict | None = None,
) -> MaterializedTable:
    # THE materializer. every table in the system — spreadsheet region, html <table>, csv, json entity — arrives here
    # as a grid plus the structure the model returned, and leaves as a MaterializedTable. one implementation, so a
    # table's shape never depends on which file it came out of. source-specific extras (a sheet's formulas, cell
    # comments, its address range) ride in as metadata rather than forking the logic
    count = structure.col_end - structure.col_start + 1
    header_rows = _plausible_header_rows(grid, structure.header_rows or [])
    # a rejected header row is data the model ate — pull data_start back so those rows are kept as rows, not lost
    rejected = set(structure.header_rows or []) - set(header_rows)
    data_start = min([structure.data_start, *rejected]) if rejected else structure.data_start
    collected: list[list[str]] = []
    for offset in range(data_start, structure.data_end + 1):
        raw = [_grid_cell(grid, offset, structure.col_start + index) for index in range(count)]
        if not any(raw):
            continue
        collected.append(raw)
    dtypes = [dtype_of([raw[index] for raw in collected]) for index in range(count)]
    headers = [_grid_header(grid, header_rows, structure.col_start + index) for index in range(count)]
    columns = [Column(header=headers[index] or f"col{index}", dtype=dtypes[index]) for index in range(count)]
    # the header rows are kept as the first rows of the stored grid — verbatim, never dropped. a row the detector
    # wrongly promoted to header survives as a queryable row; header_rows records what is header, deletion never does
    header_cells = [
        [cast_cell(_grid_cell(grid, row, structure.col_start + index)) for index in range(count)]
        for row in sorted(header_rows)
    ]
    data_rows = [[cast_cell(raw[index]) for index in range(count)] for raw in collected]
    header_indices = list(range(len(header_cells)))
    return MaterializedTable(
        sheet_no=sheet_no,
        columns=columns,
        rows=[*header_cells, *data_rows],
        sample_rows=_sample(data_rows),
        n_rows=len(data_rows),
        title=structure.title,
        caption=structure.caption,
        notes=[*(structure.notes or []), *(extra_notes or [])],
        anchors={**(anchors or {}), "header_rows": header_indices},
        formulas=formulas,
        header_rows=header_indices,
    )
