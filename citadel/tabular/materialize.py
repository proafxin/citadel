import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from citadel.schemas.table import CellValue, Column, ColumnDType, Crosstab, TableStructure

logger = logging.getLogger(__name__)

SAMPLE_TABLE_ROWS = 10

_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+\.\d+")


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


def transpose_grid(grid: list[list[str]]) -> list[list[str]]:
    # turn a sub-grid on its side. the DECISION to do so is the model's (structure.transposed); this is only the
    # mechanical flip that follows it, so a side-on table's first column becomes the header row of a normal table
    width = max((len(row) for row in grid), default=0)
    return [[_grid_cell(grid, row, col) for row in range(len(grid))] for col in range(width)]


def _grid_header(grid: list[list[str]], header_rows: list[int], col: int) -> str | None:
    parts = dict.fromkeys(cell for row in header_rows if (cell := _grid_cell(grid, row, col)))
    return " ".join(parts) or None


def _clean_name(name: str | None) -> str | None:
    # a resolved column name that is a pure decimal is a value the model read as a name (a key-value block's value
    # column). drop it so the plain col<index> fallback names the column instead. bare integers survive — a year is a
    # real column name on a wide sheet
    return None if name is not None and _lossless_decimal(name.strip()) else name


def _sample(rows: list[list[CellValue]]) -> list[list[CellValue]]:
    if len(rows) <= SAMPLE_TABLE_ROWS:
        return list(rows)
    step = len(rows) / SAMPLE_TABLE_ROWS
    return [rows[int(index * step)] for index in range(SAMPLE_TABLE_ROWS)]


def _dimension_values(grid: list[list[str]], header_row: int, start: int, end: int) -> list[str]:
    # a crosstab dimension value spans rightward across its columns even when the cells are blank — the source implied
    # the span by layout. the SLM CONFIRMED the crosstab, so carrying the last value forward is the declared meaning,
    # not a guess. returns the dimension value per value column, indexed from `start`
    out: list[str] = []
    last = ""
    for col in range(start, end + 1):
        cell = _grid_cell(grid, header_row, col).strip()
        if cell:
            last = cell
        out.append(last)
    return out


def _materialize_crosstab(
    grid: list[list[str]],
    structure: TableStructure,
    crosstab: Crosstab,
    sheet_no: int,
    formulas: list[str] | None,
    extra_notes: list[str] | None,
    anchors: dict | None,
) -> "MaterializedTable":
    # unpivot: one output row per NON-EMPTY value cell = the row's keys + that column's dimension values + the cell.
    # every emitted cell is copied from the grid; nothing is generated. empty cells carry no data, so dropping them is
    # lossless — the messy matrix becomes the same normalized relation a clean sheet would have produced
    start, end = crosstab.value_col_start, crosstab.value_col_end
    dim_values = [_dimension_values(grid, dim.header_row, start, end) for dim in crosstab.dimensions]
    out_rows: list[list[str]] = []
    for row in range(structure.data_start, structure.data_end + 1):
        keys = [_grid_cell(grid, row, key.col) for key in crosstab.key_columns]
        for col in range(start, end + 1):
            cell = _grid_cell(grid, row, col)
            if not cell.strip():
                continue
            dims = [dim_values[index][col - start] for index in range(len(crosstab.dimensions))]
            out_rows.append([*keys, *dims, cell])
    names = (
        [key.name for key in crosstab.key_columns] + [dim.name for dim in crosstab.dimensions] + [crosstab.value_name]
    )
    width = len(names)
    dtypes = [dtype_of([row[index] for row in out_rows]) for index in range(width)]
    columns = [Column(header=names[index], dtype=dtypes[index]) for index in range(width)]
    data_rows = [[cast_cell(row[index]) for index in range(width)] for row in out_rows]
    return MaterializedTable(
        sheet_no=sheet_no,
        columns=columns,
        rows=data_rows,  # no header rows: the header carried dimensions, which are now real columns on every row
        sample_rows=_sample(data_rows),
        n_rows=len(data_rows),
        title=structure.title,
        caption=structure.caption,
        notes=[*(structure.notes or []), *(extra_notes or [])],
        anchors={**(anchors or {}), "header_rows": []},
        formulas=formulas,
        header_rows=[],
    )


def _section_at(grid: list[list[str]], row: int, col_start: int, count: int) -> tuple[int, str] | None:
    # a section row the MODEL marked: its label is the first filled cell, its nesting level is that cell's column offset
    # (a full-width label starts at 0, an indented sub-section deeper). no decision here — the model already made it
    for offset in range(count):
        value = _grid_cell(grid, row, col_start + offset).strip()
        if value:
            return offset, value
    return None


def _collect_sections(
    grid: list[list[str]], data_start: int, data_end: int, col_start: int, count: int, section_rows: list[int]
) -> tuple[list[list[str]], list[list[str | None]], list[str]]:
    # apply the model's section-label calls: lift each marked row out of the data and fill its label DOWN onto the rows
    # it governs, as leading grouping column(s). one column per distinct indent level; a shallower label clears deeper
    # levels. nothing is decided or invented here — the label is source text, the membership is source structure
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
    # a multi-level header resolved for only the first of several siblings under one shared label collides here — the
    # grid's own header rows, mechanically joined per column, resolve a genuinely varying sub-label without asking the
    # model again. a name that still collides after that has no distinguishing text anywhere in the header rows —
    # inventing a suffix would hide that gap, not close it, so it is logged and left as the ambiguous name it is
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
    # SLM-resolved names win when present (it handles implied/merged/multi-row headers) — but only over a header row
    # that survived validation: a name resolved from a row that turned out to be data is not a name for anything
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
    # the header rows are kept as the first rows of the stored grid — verbatim, never dropped. a row the detector
    # wrongly promoted to header survives as a queryable row; header_rows records what is header, deletion never does
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
    # the model judged this table is on its side; turn its span back so the first column becomes the header row of a
    # normal table, then materialize that. the flip is mechanical — the decision was the model's
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
    # THE materializer. every table in the system — spreadsheet region, html <table>, csv, json entity — arrives here
    # as a grid plus the structure the model returned, and leaves as a MaterializedTable. one implementation, so a
    # table's shape never depends on which file it came out of. source-specific extras (a sheet's formulas, cell
    # comments, its address range) ride in as metadata rather than forking the logic
    if structure.transposed:
        return _materialize_transposed(grid, structure, sheet_no, formulas, extra_notes, anchors)
    if structure.layout == "crosstab" and structure.crosstab is not None:
        return _materialize_crosstab(grid, structure, structure.crosstab, sheet_no, formulas, extra_notes, anchors)
    return _materialize_relational(grid, structure, sheet_no, formulas, extra_notes, anchors)
