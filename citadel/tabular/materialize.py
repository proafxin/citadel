import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from citadel.schemas.table import CellValue, Column, ColumnDType, TableStructure

logger = logging.getLogger(__name__)

SAMPLE_TABLE_ROWS = 10

_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+\.\d+")


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
    return value or None


def _grid_cell(grid: list[list[str]], row: int, col: int) -> str:
    return grid[row][col] if 0 <= row < len(grid) and 0 <= col < len(grid[row]) else ""


def transpose_grid(grid: list[list[str]]) -> list[list[str]]:
    width = max((len(row) for row in grid), default=0)
    return [[_grid_cell(grid, row, col) for row in range(len(grid))] for col in range(width)]


def _grid_header(grid: list[list[str]], header_rows: list[int], col: int) -> str | None:
    parts = dict.fromkeys(cell for row in header_rows if (cell := _grid_cell(grid, row, col)))
    return " ".join(parts) or None


def _clean_name(name: str | None) -> str | None:
    return None if name is not None and _lossless_decimal(name.strip()) else name


def _sample(rows: list[list[CellValue]]) -> list[list[CellValue]]:
    if len(rows) <= SAMPLE_TABLE_ROWS:
        return list(rows)
    step = len(rows) / SAMPLE_TABLE_ROWS
    return [rows[int(index * step)] for index in range(SAMPLE_TABLE_ROWS)]


def _section_at(grid: list[list[str]], row: int, col_start: int, count: int) -> tuple[int, str] | None:
    for offset in range(count):
        value = _grid_cell(grid, row, col_start + offset).strip()
        if value:
            return offset, value
    return None


def _collect_sections(
    grid: list[list[str]], data_start: int, data_end: int, col_start: int, count: int, section_rows: list[int]
) -> tuple[list[list[str]], list[list[str | None]], list[str]]:
    markers: dict[int, tuple[int, str]] = {}
    for row in section_rows:
        if data_start <= row <= data_end and (marker := _section_at(grid, row, col_start, count)) is not None:
            markers[row] = marker
    levels = sorted({level for level, _ in markers.values()})
    level_index = {level: index for index, level in enumerate(levels)}
    active: list[str | None] = [None] * len(levels)
    collected: list[list[str]] = []
    sections: list[list[str | None]] = []
    for offset in range(data_start, data_end + 1):
        if offset in markers:
            level, label = markers[offset]
            index = level_index[level]
            active[index] = label
            for deeper in range(index + 1, len(active)):
                active[deeper] = None
            continue
        raw = [_grid_cell(grid, offset, col_start + index) for index in range(count)]
        if not any(raw):
            continue
        collected.append(raw)
        sections.append(list(active))
    names = ["section"] if len(levels) == 1 else [f"section_{index + 1}" for index in range(len(levels))]
    return collected, sections, names


def _section_columns(sections: list[list[str | None]], names: list[str]) -> list[Column]:
    return [
        Column(header=names[index], dtype=dtype_of([section[index] or "" for section in sections]))
        for index in range(len(names))
    ]


def _dedupe_headers(
    grid: list[list[str]], header_rows: list[int], col_start: int, headers: list[str | None]
) -> list[str | None]:
    counts = Counter(name for name in headers if name)
    resolved: list[str | None] = []
    for index, name in enumerate(headers):
        if name and counts[name] > 1:
            resolved.append(_grid_header(grid, header_rows, col_start + index) or name)
        else:
            resolved.append(name)
    counts = Counter(name for name in resolved if name)
    duplicates = sorted({name for name, count in counts.items() if count > 1})
    if duplicates:
        logger.warning("materialize duplicate column names survive grid fallback: %s", duplicates)
    return resolved


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
    collected, sections, section_names = _collect_sections(
        grid, structure.data_start, structure.data_end, structure.col_start, count, structure.section_rows or []
    )
    dtypes = [dtype_of([raw[index] for raw in collected]) for index in range(count)]
    if structure.columns and header_rows:
        headers: list[str | None] = [
            structure.columns[index] if index < len(structure.columns) else None for index in range(count)
        ]
    else:
        headers = [_grid_header(grid, header_rows, structure.col_start + index) for index in range(count)]
    headers = _dedupe_headers(grid, header_rows, structure.col_start, headers)
    columns = [
        Column(header=_clean_name(headers[index]) or f"col{index}", dtype=dtypes[index]) for index in range(count)
    ]
    section_columns = _section_columns(sections, section_names)
    header_pad: list[CellValue] = [None] * len(section_names)
    header_cells = [
        [*header_pad, *(cast_cell(_grid_cell(grid, row, structure.col_start + index)) for index in range(count))]
        for row in sorted(header_rows)
    ]
    data_rows = [
        [
            *(cast_cell(sections[position][index] or "") for index in range(len(section_names))),
            *(cast_cell(collected[position][index]) for index in range(count)),
        ]
        for position in range(len(collected))
    ]
    header_indices = list(range(len(header_cells)))
    return MaterializedTable(
        sheet_no=sheet_no,
        columns=[*section_columns, *columns],
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
