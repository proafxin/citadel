import logging
from collections.abc import Sequence
from datetime import datetime

from citadel.llm import call_structured, count_tokens, count_tokens_batch
from citadel.prompts import load_prompt
from citadel.schemas.table import TableStructure
from citadel.services.capacity import get_text_capacity
from citadel.tabular.flag import SAMPLE_BOTTOM, SAMPLE_TOP, column_kinds, payload_rows
from citadel.tabular.materialize import MaterializedTable, materialize, sample_rows

logger = logging.getLogger(__name__)

_MAX_CELL = 40
SINGLE_TABLE_BUDGET = 8192
WINDOW_BUDGET = 2048
_HEADER_ALLOWANCE = 64

_EXTRACT_ALL_SCHEMA = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_row": {"type": "integer"},
                    "end_row": {"type": "integer"},
                    "start_col": {"type": "integer"},
                    "end_col": {"type": "integer"},
                    "header_rows": {"type": "array", "items": {"type": "integer"}},
                    "data_start": {"type": "integer"},
                    "data_end": {"type": "integer"},
                    "metadata_rows": {"type": "array", "items": {"type": "integer"}},
                    "title_row": {"type": ["integer", "null"]},
                },
                "required": [
                    "start_row",
                    "end_row",
                    "start_col",
                    "end_col",
                    "header_rows",
                    "data_start",
                    "data_end",
                    "metadata_rows",
                    "title_row",
                ],
            },
        }
    },
    "required": ["tables"],
}

_TABLE_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "header_rows": {"type": "array", "items": {"type": "integer"}},
        "transposed": {"type": "boolean"},
        "data_start": {"type": "integer"},
        "data_end": {"type": "integer"},
        "col_start": {"type": "integer"},
        "col_end": {"type": "integer"},
        "columns": {"type": "array", "items": {"type": "string"}},
        "title": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "header_rows",
        "transposed",
        "data_start",
        "data_end",
        "col_start",
        "col_end",
        "columns",
        "title",
        "notes",
    ],
}
_TABLE_STRUCTURE_SCHEMA = {
    "type": "object",
    "properties": {"tables": {"type": "array", "items": _TABLE_ENTRY_SCHEMA}},
    "required": ["tables"],
}


def _row_line(index: int, row: list[str], width: int, max_cell: int | None, note: str = "") -> str:
    cells = [(row[col] if col < len(row) else "").strip() for col in range(width)]
    if max_cell is not None:
        cells = [cell[:max_cell] for cell in cells]
    return f"{index}{note}: " + " | ".join(cells)


def _payload_text(grid: list[list[str]], indices: list[int], width: int, max_cell: int | None) -> str:
    return "\n".join(_row_line(index, grid[index], width, max_cell) for index in indices)


MAX_SAMPLE_ROWS = 50


def _stride(rows: list[int], room: int) -> list[int]:
    if room <= 0 or not rows:
        return []
    if room >= len(rows):
        return rows
    step = len(rows) / room
    return sorted({rows[int(index * step)] for index in range(room)})


def _budgeted_sample(grid: list[list[str]], width: int, budget: int) -> list[int]:
    rows = list(range(len(grid)))
    if not rows:
        return []
    lines = [_row_line(index, grid[index], width, _MAX_CELL) for index in rows]
    counts = count_tokens_batch(lines)
    if sum(counts) <= budget:
        return rows
    avg = sum(counts) / len(counts)
    room = min(max(int(budget / avg), SAMPLE_TOP + SAMPLE_BOTTOM), MAX_SAMPLE_ROWS)
    edges = set(rows[:SAMPLE_TOP]) | set(rows[len(rows) - SAMPLE_BOTTOM :])
    middle = [index for index in rows[SAMPLE_TOP : len(rows) - SAMPLE_BOTTOM] if index not in edges]
    return sorted(edges | set(_stride(middle, max(room - len(edges), 0))))


def _candidate_text(grid: list[list[str]], *, full: bool, budget: int | None) -> str:
    width = max((len(row) for row in grid), default=0)
    kinds = column_kinds(grid)
    hint = ", ".join(f"col{col}:{kinds[col]}" for col in range(width))
    header = f"{len(grid)} rows, {width} cols; column kinds: {hint}"
    if full:
        rows = list(range(len(grid)))
    elif budget is not None:
        row_budget = max(budget - count_tokens(header), 0)
        rows = _budgeted_sample(grid, width, row_budget)
    else:
        rows = payload_rows(grid)
    body = _payload_text(grid, rows, width, None if full else _MAX_CELL)
    return f"{header}\n{body}"


def _bounds(entry: dict) -> tuple[int, int] | None:
    try:
        return int(entry["data_start"]), int(entry["data_end"])
    except (KeyError, ValueError, TypeError):
        return None


def _cols_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def _non_overlapping(entries: list[dict]) -> list[dict]:
    valid = [(entry, bounds) for entry in entries if (bounds := _bounds(entry)) is not None]
    ordered = sorted(valid, key=lambda pair: pair[1][0])
    claimed: list[tuple[int, int, int]] = []
    result: list[dict] = []
    for entry, (data_start, data_end) in ordered:
        col_range = _entry_col_range(entry)
        claimed_until = max(
            (until for c_start, c_end, until in claimed if _cols_overlap(col_range, (c_start, c_end))),
            default=-1,
        )
        start = max(data_start, claimed_until + 1)
        if start > data_end:
            continue
        result.append({**entry, "data_start": start, "data_end": data_end})
        claimed.append((*col_range, data_end))
    return result


def _entry_col_range(entry: dict) -> tuple[int, int]:
    return int(entry["col_start"]), int(entry["col_end"])


STAGE2_MAX_TOKENS = 4096


def _title_in_columns(grid: list[list[str]], row: object, data_start: int, col_start: int, col_end: int) -> str | None:
    if not isinstance(row, int) or not 0 <= row < len(grid) or row >= data_start:
        return None
    cells = grid[row][col_start : col_end + 1]
    parts = [cell.strip() for cell in cells if cell.strip()]
    return " ".join(dict.fromkeys(parts)) or None


def _bold_note(bold: Sequence[Sequence[bool]] | None, index: int) -> str:
    if bold is None or not 0 <= index < len(bold):
        return ""
    marked = [str(col) for col, flag in enumerate(bold[index]) if flag]
    return f" [bold {','.join(marked)}]" if marked else ""


def _structure_render(
    grid: list[list[str]], start: int, bold: Sequence[Sequence[bool]] | None = None
) -> tuple[str, int]:
    width = max((len(row) for row in grid), default=0)
    lines = [_row_line(index, grid[index], width, None, _bold_note(bold, index)) for index in range(start, len(grid))]
    budget = max(WINDOW_BUDGET - _HEADER_ALLOWANCE, 0)
    used = 0
    kept: list[str] = []
    for line, cost in zip(lines, count_tokens_batch(lines), strict=True):
        if kept and used + cost > budget:
            break
        kept.append(line)
        used += cost
    last = start + len(kept) - 1
    if last < len(grid) - 1:
        header = f"rows {start}-{last} of {len(grid)} rows in this region, {width} cols"
    else:
        header = f"{len(grid)} rows, {width} cols"
    return f"{header}\n" + "\n".join(kept), last


def _value_class(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, datetime):
        return "temporal"
    return "string" if str(value).strip() else None


def _row_types(values: Sequence[Sequence[object]], row: int) -> dict[int, str]:
    if not 0 <= row < len(values):
        return {}
    return {col: kind for col, cell in enumerate(values[row]) if (kind := _value_class(cell)) is not None}


def _schema(values: Sequence[Sequence[object]], first: int, last: int) -> dict[int, str]:
    profile: dict[int, str] = {}
    for row in range(first, min(last, len(values) - 1) + 1):
        for col, kind in _row_types(values, row).items():
            profile.setdefault(col, kind)
    return profile


def _conforms(schema: dict[int, str], row: dict[int, str]) -> bool:
    if not row:
        return False
    return all(schema.get(col) in {None, kind} for col, kind in row.items())


def _extend(values: Sequence[Sequence[object]], schema: dict[int, str], start: int) -> int:
    row = start
    while row < len(values) and _conforms(schema, _row_types(values, row)):
        row += 1
    return row - 1


def _entry_structure(grid: list[list[str]], entry: dict, width: int, *, single: bool) -> TableStructure | None:
    try:
        data_start = int(entry["data_start"])
        data_end = int(entry["data_end"])
    except (KeyError, ValueError, TypeError):
        return None
    limit = max(len(grid) - 1, 0)
    last_col = max(width - 1, 0)
    data_start = min(max(data_start, 0), limit)
    data_end = min(max(data_end, data_start), limit)
    if single:
        col_start, col_end = 0, last_col
    else:
        col_start = min(max(int(entry.get("start_col") or 0), 0), last_col)
        col_end = min(max(int(entry.get("end_col") or last_col), col_start), last_col)
    headers = sorted({row for row in entry.get("header_rows") or [] if 0 <= row <= limit})
    metadata = sorted({row for row in entry.get("metadata_rows") or [] if data_start <= row <= data_end})
    return TableStructure(
        col_start=col_start,
        col_end=col_end,
        header_rows=headers or None,
        data_start=data_start,
        data_end=data_end,
        metadata_rows=metadata or None,
        title=_title_in_columns(grid, entry.get("title_row"), data_start, col_start, col_end),
    )


def _entry_anchors(base: dict | None, entry: dict, structure: TableStructure) -> dict:
    known = base or {}
    row0, col0 = known.get("min_row", 0), known.get("min_col", 0)
    rows = [structure.data_start, *(structure.header_rows or []), *(structure.metadata_rows or [])]
    title_row = entry.get("title_row")
    if isinstance(title_row, int) and title_row < structure.data_start:
        rows.append(title_row)
    return {
        **known,
        "min_row": row0 + min(rows),
        "max_row": row0 + max(structure.data_end, *(structure.metadata_rows or [structure.data_end])),
        "min_col": col0 + structure.col_start,
        "max_col": col0 + structure.col_end,
    }


async def _window_entries(
    grid: list[list[str]], start: int, key: str, bold: Sequence[Sequence[bool]] | None
) -> tuple[list[dict], int]:
    text, last = _structure_render(grid, start, bold)
    cap = get_text_capacity()
    await cap.acquire(key)
    try:
        data = await call_structured(f"{load_prompt('table_extract_all')}\n\n{text}", _EXTRACT_ALL_SCHEMA)
    finally:
        await cap.release(key)
    return data.get("tables") or [], last


async def _structure_excel(
    grid: list[list[str]],
    values: Sequence[Sequence[object]] | None,
    *,
    key: str,
    sheet_no: int,
    anchors: dict | None,
    bold: Sequence[Sequence[bool]] | None,
) -> list[MaterializedTable]:
    width = max((len(row) for row in grid), default=0)
    collected: list[dict] = []
    start = 0
    window = 0
    while start < len(grid):
        entries, last = await _window_entries(grid, start, f"{key}:w{window}", bold)
        window += 1
        if not entries:
            break
        collected.extend(entries)
        if last >= len(grid) - 1:
            break
        tail = max((int(entry.get("data_end") or 0) for entry in entries), default=last)
        if values is None:
            start = last + 1
            continue
        schema = _schema(values, max(int(entries[-1].get("data_start") or 0), 0), tail)
        reach = _extend(values, schema, last + 1)
        entries[-1]["data_end"] = max(tail, reach)
        start = reach + 1

    single = len(collected) == 1
    tables: list[MaterializedTable] = []
    for entry in collected:
        structure = _entry_structure(grid, entry, width, single=single)
        if structure is None:
            continue
        table = materialize(grid, structure, sheet_no=sheet_no, anchors=_entry_anchors(anchors, entry, structure))
        if table.n_rows:
            tables.append(table)
    return tables


_RECONCILE_SCHEMA = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "members": {"type": "array", "items": {"type": "integer"}},
                    "header_from": {"type": ["integer", "null"]},
                    "title": {"type": ["string", "null"]},
                },
                "required": ["members", "header_from", "title"],
            },
        }
    },
    "required": ["tables"],
}

_MIN_RECONCILE_TABLES = 2


def _reconcile_entry(index: int, table: MaterializedTable) -> str:
    anchors = table.anchors
    columns = ", ".join(f"{column.header}:{column.dtype}" for column in table.columns)
    lines = [
        (
            f"[{index}] sheet rows {anchors.get('min_row')}-{anchors.get('max_row')}, "
            f"cols {anchors.get('min_col')}-{anchors.get('max_col')}; {table.n_rows} rows; title={table.title!r}"
        ),
        f"    columns: {columns}",
    ]
    if table.sample_rows:
        lines.append("    row: " + " | ".join(str(cell) for cell in table.sample_rows[0]))
    return "\n".join(lines)


def _join(members: list[MaterializedTable], header: MaterializedTable, title: str | None) -> MaterializedTable:
    lead = members[0]
    data = [row for table in members for row in table.rows[len(table.header_rows) :]]
    header_cells = header.rows[: len(header.header_rows)]
    return MaterializedTable(
        sheet_no=lead.sheet_no,
        columns=header.columns,
        rows=[*header_cells, *data],
        sample_rows=sample_rows(data),
        n_rows=len(data),
        title=title or lead.title,
        caption=lead.caption,
        notes=lead.notes,
        anchors={
            **lead.anchors,
            "min_row": min(table.anchors.get("min_row", 0) for table in members),
            "max_row": max(table.anchors.get("max_row", 0) for table in members),
            "min_col": min(table.anchors.get("min_col", 0) for table in members),
            "max_col": max(table.anchors.get("max_col", 0) for table in members),
        },
        formulas=lead.formulas,
        header_rows=list(range(len(header_cells))),
    )


def _structure(entry: dict) -> TableStructure:
    col_start, col_end = _entry_col_range(entry)
    return TableStructure(
        col_start=col_start,
        col_end=col_end,
        header_rows=list(entry.get("header_rows") or []) or None,
        transposed=bool(entry.get("transposed")),
        data_start=int(entry["data_start"]),
        data_end=int(entry["data_end"]),
        columns=list(entry.get("columns") or []) or None,
        title=entry.get("title") or None,
        notes=list(entry.get("notes") or []) or None,
    )


async def structure_candidate(
    grid: list[list[str]],
    *,
    key: str,
    full: bool = False,
    known_table: bool = True,
    sheet_no: int = 0,
    anchors: dict | None = None,
    values: Sequence[Sequence[object]] | None = None,
    bold: Sequence[Sequence[bool]] | None = None,
) -> list[MaterializedTable]:
    if not grid:
        return []
    if not known_table:
        return await _structure_excel(grid, values, key=key, sheet_no=sheet_no, anchors=anchors, bold=bold)
    text = _candidate_text(grid, full=full, budget=None if full else SINGLE_TABLE_BUDGET)
    prompt = f"{load_prompt('table_structure_known')}\n{text}"
    cap = get_text_capacity()
    await cap.acquire(key)
    try:
        data = await call_structured(prompt, _TABLE_STRUCTURE_SCHEMA)
    finally:
        await cap.release(key)
    tables: list[MaterializedTable] = []
    for entry in _non_overlapping(data.get("tables") or []):
        try:
            structure = _structure(entry)
        except (KeyError, ValueError, TypeError):
            logger.warning("structure_candidate dropped malformed entry=%r", entry)
            continue
        table = materialize(grid, structure, sheet_no=sheet_no, anchors=anchors)
        if table.n_rows:
            tables.append(table)
    return tables
