import asyncio
import logging
from collections import Counter
from datetime import datetime
from itertools import starmap
from typing import Protocol

from citadel.llm import call_structured, call_text, count_tokens, count_tokens_batch
from citadel.prompts import load_prompt
from citadel.schemas.table import TableStructure
from citadel.services.capacity import get_text_capacity
from citadel.tabular.flag import SAMPLE_BOTTOM, SAMPLE_TOP, column_kinds, payload_rows
from citadel.tabular.materialize import MaterializedTable, materialize, sample_rows

logger = logging.getLogger(__name__)


class Position(Protocol):
    min_row: int
    max_row: int
    min_col: int
    max_col: int


_MAX_CELL = 40
SINGLE_TABLE_BUDGET = 8192
EXCEL_CHUNK_BUDGET = 8192

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
_MERGE_SCHEMA = {
    "type": "object",
    "properties": {"groups": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}},
    "required": ["groups"],
}


def _row_line(index: int, row: list[str], width: int, max_cell: int | None) -> str:
    cells = [(row[col] if col < len(row) else "").strip() for col in range(width)]
    if max_cell is not None:
        cells = [cell[:max_cell] for cell in cells]
    return f"{index}: " + " | ".join(cells)


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


STAGE1_MAX_TOKENS = 2048
STAGE2_MAX_TOKENS = 4096


def _row_line_excel(index: int, row: list[str], width: int) -> str:
    cells = [(row[col] if col < len(row) else "").strip() for col in range(width)]
    return f"row {index}: " + " | ".join(f"col{col}: {cell}" for col, cell in enumerate(cells))


def _render_excel(grid: list[list[str]], rows: list[int], width: int) -> str:
    if len(rows) < len(grid):
        header = f"showing rows {rows[0]}-{rows[-1]} ({len(rows)} of {len(grid)} rows in the full sheet region), {width} cols"
    else:
        header = f"{len(grid)} rows, {width} cols"
    lines = [_row_line_excel(index, grid[index], width) for index in rows]
    return "\n".join([header, *lines])


def _row_line_markdown(index: int, row: list[str], width: int) -> str:
    cells = [(row[col] if col < len(row) else "").strip() for col in range(width)]
    return f"| {index} | " + " | ".join(cells) + " |"


def _render_markdown(grid: list[list[str]], rows: list[int], width: int) -> str:
    header = f"{len(grid)} rows, {width} cols\n"
    header += "| row | " + " | ".join(f"c{col}" for col in range(width)) + " |\n"
    header += "|---" * (width + 1) + "|"
    lines = [_row_line_markdown(index, grid[index], width) for index in rows]
    return "\n".join([header, *lines])


def _chunk_rows(grid: list[list[str]], width: int, budget: int) -> list[list[int]]:
    rows = list(range(len(grid)))
    if not rows:
        return []
    lines = [_row_line_excel(index, grid[index], width) for index in rows]
    counts = count_tokens_batch(lines)
    chunks: list[list[int]] = []
    current: list[int] = []
    current_tokens = 0
    for index, tokens in zip(rows, counts, strict=True):
        if current and current_tokens + tokens > budget:
            chunks.append(current)
            current, current_tokens = [], 0
        current.append(index)
        current_tokens += tokens
    if current:
        chunks.append(current)
    return chunks


def _render_excel_chunks(grid: list[list[str]], *, budget: int) -> list[str]:
    width = max((len(row) for row in grid), default=0)
    chunks = _chunk_rows(grid, width, budget)
    return [_render_excel(grid, rows, width) for rows in chunks]


def _slice_grid(grid: list[list[str]], min_row: int, max_row: int, min_col: int, max_col: int) -> list[list[str]]:
    return [row[min_col : max_col + 1] for row in grid[min_row : max_row + 1]]


_STAGE1_EXTRACT_SCHEMA = {
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
                },
                "required": ["start_row", "end_row", "start_col", "end_col"],
            },
        }
    },
    "required": ["tables"],
}


async def _detect_boundaries_chunk(
    text: str, *, key: str, prompt_name: str = "table_structure_excel"
) -> list[tuple[int, int, int, int]]:
    prompt = f"{load_prompt(prompt_name)}\n\n{text}"
    reasoning = await call_text(prompt, max_tokens=STAGE1_MAX_TOKENS, key=key)
    extract_prompt = f"{load_prompt('table_structure_extract')}\n\n{reasoning}"
    cap = get_text_capacity()
    await cap.acquire(f"{key}:extract")
    try:
        data = await call_structured(extract_prompt, _STAGE1_EXTRACT_SCHEMA)
    finally:
        await cap.release(f"{key}:extract")
    boxes: list[tuple[int, int, int, int]] = []
    for entry in data.get("tables") or []:
        try:
            box = (int(entry["start_row"]), int(entry["end_row"]), int(entry["start_col"]), int(entry["end_col"]))
        except (KeyError, ValueError, TypeError):
            logger.warning("stage1 dropped malformed entry=%r", entry)
            continue
        boxes.append(box)
    return boxes


async def _detect_boundaries(grid: list[list[str]], *, key: str) -> list[tuple[int, int, int, int]]:
    chunks = _render_excel_chunks(grid, budget=EXCEL_CHUNK_BUDGET)
    results = await asyncio.gather(
        *(_detect_boundaries_chunk(text, key=f"{key}:chunk{index}") for index, text in enumerate(chunks))
    )
    return [box for boxes in results for box in boxes]


SCAN_CONTEXT = 4


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


def _row_types(values: list[list[object]], index: int) -> dict[int, str]:
    if not 0 <= index < len(values):
        return {}
    return {col: cls for col, value in enumerate(values[index]) if (cls := _value_class(value)) is not None}


def _scan_profile(values: list[list[object]], lo: int, hi: int) -> dict[int, str]:
    counts: dict[int, Counter[str]] = {}
    for index in range(lo, min(hi + 1, len(values))):
        for col, cls in _row_types(values, index).items():
            counts.setdefault(col, Counter())[cls] += 1
    return {col: counter.most_common(1)[0][0] for col, counter in counts.items()}


def _scan_conforms(types: dict[int, str], extent: frozenset[int], row: dict[int, str]) -> bool:
    if not row or not set(row) <= extent:
        return False
    return all(types.get(col) in {None, cls} for col, cls in row.items())


def _first_data_row(values: list[list[object]], start: int, last: int, extent: frozenset[int]) -> int:
    if last <= start:
        return start
    midpoint = start + (last - start) // 2 + 1
    seed = _scan_profile(values, midpoint, last)
    if not seed:
        return min(start + 1, last)
    row = midpoint
    while row > start and _scan_conforms(seed, extent, _row_types(values, row - 1)):
        row -= 1
    return row


def _scan_window(grid: list[list[str]], start: int, width: int) -> tuple[str, int] | None:
    rows: list[int] = []
    used = 0
    for index in range(start, len(grid)):
        cost = count_tokens(_row_line_excel(index, grid[index], width))
        if used + cost > EXCEL_CHUNK_BUDGET and rows:
            break
        rows.append(index)
        used += cost
    if not rows:
        return None
    return _render_excel(grid, rows, width), rows[-1]


def _starts_to_boxes(starts: list[tuple[int, int, int]], rows: int) -> list[tuple[int, int, int, int]]:
    ordered = sorted(set(starts))
    boxes: list[tuple[int, int, int, int]] = []
    for position, (start, col_start, col_end) in enumerate(ordered):
        later = [other for other, _a, _b in ordered[position + 1 :] if other > start]
        end = min(later) - 1 if later else rows - 1
        if end >= start:
            boxes.append((start, end, col_start, col_end))
    return boxes


async def _anchor_boundaries(
    grid: list[list[str]], values: list[list[object]], *, key: str
) -> tuple[list[tuple[int, int, int, int]], list[int]]:
    width = max((len(row) for row in grid), default=0)
    queue: list[tuple[int, int]] = [(0, 0)]
    seen: set[int] = set()
    starts: list[tuple[int, int, int]] = []
    unexplained: list[int] = []
    while queue:
        batch = [(window, claim) for window, claim in queue if claim not in seen]
        seen.update(claim for _window, claim in batch)
        queue = []
        rendered = [(claim, _scan_window(grid, window, width)) for window, claim in batch]
        pending = [(claim, window) for claim, window in rendered if window is not None]
        if not pending:
            break
        results = await asyncio.gather(
            *(
                _detect_boundaries_chunk(text, key=f"{key}:at{claim}", prompt_name="table_structure_anchor")
                for claim, (text, _last) in pending
            )
        )
        for (claim, (_text, last)), boxes in zip(pending, results, strict=True):
            accepted = False
            for start_row, _end_row, col_start, col_end in boxes:
                if start_row < claim or start_row >= len(grid):
                    continue
                accepted = True
                starts.append((start_row, col_start, col_end))
                extent = frozenset(range(col_start, col_end + 1))
                types = _scan_profile(values, _first_data_row(values, start_row, last, extent), last)
                breaks = [
                    index
                    for index in range(last + 1, len(grid))
                    if not _scan_conforms(types, extent, _row_types(values, index))
                ]
                if breaks:
                    queue.append((max(breaks[0] - SCAN_CONTEXT, 0), breaks[0]))
            if not accepted and claim > 0:
                unexplained.append(claim)
    return _starts_to_boxes(starts, len(grid)), sorted(unexplained)


def _title_from_row(grid: list[list[str]], data: dict, data_start: int) -> str | None:
    row = data.get("title_row")
    if not isinstance(row, int) or not 0 <= row < len(grid) or row >= data_start:
        return None
    parts = [cell.strip() for cell in grid[row] if cell.strip()]
    return " ".join(dict.fromkeys(parts)) or None


def _stage2_structure(data: dict, width: int, grid: list[list[str]]) -> TableStructure | None:
    try:
        data_start, data_end = int(data["data_start"]), int(data["data_end"])
    except (KeyError, ValueError, TypeError):
        return None
    header_rows = [int(row) for row in data.get("header_rows") or []]
    metadata_rows = [int(row) for row in data.get("metadata_rows") or [] if data_start <= int(row) <= data_end]
    return TableStructure(
        col_start=0,
        col_end=max(width - 1, 0),
        header_rows=header_rows or None,
        data_start=data_start,
        data_end=data_end,
        metadata_rows=metadata_rows or None,
        title=_title_from_row(grid, data, data_start),
    )


_TYPED_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "header_rows": {"type": "array", "items": {"type": "integer"}},
        "data_start": {"type": "integer"},
        "data_end": {"type": "integer"},
        "metadata_rows": {"type": "array", "items": {"type": "integer"}},
        "title_row": {"type": ["integer", "null"]},
    },
    "required": ["header_rows", "data_start", "data_end", "metadata_rows", "title_row"],
}


def _typing_rows(grid: list[list[str]], width: int) -> list[int]:
    rows = list(range(len(grid)))
    if not rows:
        return rows
    counts = count_tokens_batch([_row_line_excel(index, grid[index], width) for index in rows])
    if sum(counts) <= EXCEL_CHUNK_BUDGET:
        return rows
    half = EXCEL_CHUNK_BUDGET // 2
    head: list[int] = []
    used = 0
    for index, cost in zip(rows, counts, strict=True):
        if used + cost > half:
            break
        head.append(index)
        used += cost
    tail: list[int] = []
    used = 0
    for index, cost in zip(reversed(rows), reversed(counts), strict=True):
        if used + cost > half or index in set(head):
            break
        tail.append(index)
        used += cost
    return head + sorted(tail)


async def _type_rows(grid: list[list[str]], *, key: str, anomalies: list[int] | None = None) -> TableStructure | None:
    width = max((len(row) for row in grid), default=0)
    rows = _typing_rows(grid, width)
    text = _render_excel(grid, rows, width)
    if anomalies:
        text += f"\n\nrows whose values do not match the column types of the surrounding rows: {anomalies}"
    prompt = f"{load_prompt('table_structure_typed')}\n\n{text}"
    reasoning = await call_text(prompt, max_tokens=STAGE2_MAX_TOKENS, key=key)
    if len(rows) < len(grid):
        extract_prompt = f"{load_prompt('table_typed_extract')}\n\n{reasoning}"
    else:
        extract_prompt = f"{load_prompt('table_typed_extract_lines')}\n\n{text}\n\n{reasoning}"
    cap = get_text_capacity()
    await cap.acquire(f"{key}:extract")
    try:
        data = await call_structured(extract_prompt, _TYPED_EXTRACT_SCHEMA)
    finally:
        await cap.release(f"{key}:extract")
    return _stage2_structure(data, width, grid)


def _scan_anomalies(values: list[list[object]], box: tuple[int, int, int, int]) -> list[int]:
    start_row, end_row, col_start, col_end = box
    extent = frozenset(range(col_start, col_end + 1))
    midpoint = start_row + (end_row - start_row) // 2
    types = _scan_profile(values, midpoint, end_row)
    if not types:
        return []
    return [
        row - start_row
        for row in range(start_row, end_row + 1)
        if not _scan_conforms(types, extent, _row_types(values, row))
    ]


def _combine_structure(
    grid: list[list[str]], box: tuple[int, int, int, int], typed: TableStructure | None, rows: int
) -> TableStructure | None:
    if typed is None:
        return None
    _start_row, _end_row, col_start, col_end = box
    limit = max(rows - 1, 0)
    headers = sorted({row for row in (typed.header_rows or []) if 0 <= row <= limit})
    data_start = min(max(typed.data_start, 0), limit)
    metadata = sorted({row for row in (typed.metadata_rows or []) if 0 <= row <= limit})
    return TableStructure(
        col_start=0,
        col_end=col_end - col_start,
        header_rows=headers or None,
        data_start=data_start,
        data_end=limit,
        metadata_rows=[row for row in metadata if row >= data_start and row not in set(headers)] or None,
        title=typed.title or None,
    )


def _preamble_title(grid: list[list[str]], box: tuple[int, int, int, int], structure: TableStructure) -> str | None:
    start_row = box[0]
    headers = {start_row + row for row in (structure.header_rows or [])}
    limit = min(start_row + structure.data_start, len(grid))
    parts = [
        cell.strip() for row in range(start_row, limit) if row not in headers for cell in grid[row] if cell.strip()
    ]
    return " ".join(dict.fromkeys(parts)) or None


def _apply_scan(
    structure: TableStructure, box: tuple[int, int, int, int], scanned: list[int], rows: int
) -> TableStructure:
    start_row, end_row, _col_start, _col_end = box
    local = sorted({row - start_row for row in scanned if start_row <= row <= end_row})
    merged = sorted(set(structure.metadata_rows or []) | set(local))
    return structure.model_copy(
        update={
            "data_end": max(rows - 1, structure.data_start),
            "metadata_rows": merged or None,
        }
    )


async def _structure_excel(
    grid: list[list[str]],
    values: list[list[object]] | None,
    *,
    key: str,
    sheet_no: int,
    anchors: dict | None,
) -> list[MaterializedTable]:
    width = max((len(row) for row in grid), default=0)
    scanned: list[int] = []
    if values is not None and len(_chunk_rows(grid, width, EXCEL_CHUNK_BUDGET)) > 1:
        boxes, scanned = await _anchor_boundaries(grid, values, key=f"{key}:scan")
    else:
        boxes = await _detect_boundaries(grid, key=f"{key}:stage1")
    if not boxes:
        boxes = [(0, len(grid) - 1, 0, max(width - 1, 0))]
    subgrids = [_slice_grid(grid, *box) for box in boxes]
    anomalies = [_scan_anomalies(values, box) if values is not None else [] for box in boxes]
    typed = list(
        await asyncio.gather(
            *(
                _type_rows(subgrid, key=f"{key}:stage2:{index}", anomalies=found)
                for index, (subgrid, found) in enumerate(zip(subgrids, anomalies, strict=True))
            )
        )
    )
    structures: list[TableStructure | None] = [
        _combine_structure(grid, box, entry, len(subgrid))
        for box, subgrid, entry in zip(boxes, subgrids, typed, strict=True)
    ]
    tables: list[MaterializedTable] = []
    for box, subgrid, structure in zip(boxes, subgrids, structures, strict=True):
        if structure is None:
            continue
        if scanned:
            structure = _apply_scan(structure, box, scanned, len(subgrid))
        base = anchors or {}
        row0, col0 = base.get("min_row", 0), base.get("min_col", 0)
        scoped = {
            **base,
            "min_row": row0 + box[0],
            "max_row": row0 + box[1],
            "min_col": col0 + box[2],
            "max_col": col0 + box[3],
        }
        table = materialize(subgrid, structure, sheet_no=sheet_no, anchors=scoped)
        if table.n_rows:
            tables.append(table)
    return tables


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
    values: list[list[object]] | None = None,
) -> list[MaterializedTable]:
    if not grid:
        return []
    if not known_table:
        return await _structure_excel(grid, values, key=key, sheet_no=sheet_no, anchors=anchors)
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


def _candidate_summary(index: int, table: MaterializedTable) -> str:
    columns = ", ".join(f"{column.header}:{column.dtype}" for column in table.columns)
    title = f"; title: {table.title}" if table.title else ""
    return f"{index}: {table.n_rows} rows; columns: {columns}{title}"


async def _resolve_group(
    doc_id: str, sheet_no: int, group_no: int, members: list[MaterializedTable]
) -> list[list[int]]:
    listing = "\n".join(starmap(_candidate_summary, enumerate(members)))
    prompt = f"{load_prompt('table_merge_candidates')}\n{listing}"
    key = f"merge:{doc_id}:sheet{sheet_no}:{group_no}"
    cap = get_text_capacity()
    await cap.acquire(key)
    try:
        data = await call_structured(prompt, _MERGE_SCHEMA)
    finally:
        await cap.release(key)
    groups = data.get("groups") or []
    logger.info("merge sheet%d groups=%s", sheet_no, groups)
    seen: set[int] = set()
    valid: list[list[int]] = []
    for chain in groups:
        indices = [
            index for index in chain if isinstance(index, int) and 0 <= index < len(members) and index not in seen
        ]
        if not indices:
            continue
        seen.update(indices)
        valid.append(indices)
    valid.extend([index] for index in range(len(members)) if index not in seen)
    return valid


def _combine(members: list[MaterializedTable], chain: list[int]) -> MaterializedTable:
    leader = members[chain[0]]
    rows = list(leader.rows)
    for index in chain[1:]:
        table = members[index]
        rows.extend(table.rows[len(table.header_rows) :])
    data_rows = rows[len(leader.header_rows) :]
    return MaterializedTable(
        sheet_no=leader.sheet_no,
        columns=leader.columns,
        rows=rows,
        sample_rows=sample_rows(data_rows),
        n_rows=len(data_rows),
        title=leader.title,
        caption=leader.caption,
        notes=leader.notes,
        anchors=leader.anchors,
        formulas=leader.formulas,
        header_rows=leader.header_rows,
    )


_MIN_CHAIN = 2


def _adjacency_groups(regions: list[Position]) -> list[list[int]]:
    row_order = sorted(range(len(regions)), key=lambda i: regions[i].min_row)
    rank = {index: position for position, index in enumerate(row_order)}
    by_columns: dict[tuple[int, int], list[int]] = {}
    for index, region in enumerate(regions):
        by_columns.setdefault((region.min_col, region.max_col), []).append(index)
    groups: list[list[int]] = []
    for indices in by_columns.values():
        if len(indices) < _MIN_CHAIN:
            continue
        ordered = sorted(indices, key=lambda i: regions[i].min_row)
        chain = [ordered[0]]
        for index in ordered[1:]:
            if rank[index] == rank[chain[-1]] + 1:
                chain.append(index)
                continue
            if len(chain) >= _MIN_CHAIN:
                groups.append(chain)
            chain = [index]
        if len(chain) >= _MIN_CHAIN:
            groups.append(chain)
    return groups


async def merge_candidates(
    doc_id: str, sheet_no: int, regions: list[Position], members: list[MaterializedTable]
) -> list[MaterializedTable]:
    if len(regions) != len(members):
        msg = f"regions/members length mismatch: {len(regions)} vs {len(members)}"
        raise ValueError(msg)
    if not members:
        return []
    groups = _adjacency_groups(regions)
    grouped_indices: set[int] = {index for group in groups for index in group}
    out: list[MaterializedTable] = [table for index, table in enumerate(members) if index not in grouped_indices]
    group_members_list = [[members[index] for index in group] for group in groups]
    resolved = await asyncio.gather(
        *(
            _resolve_group(doc_id, sheet_no, group_no, group_members)
            for group_no, group_members in enumerate(group_members_list)
        )
    )
    for group_members, chains in zip(group_members_list, resolved, strict=True):
        out.extend(group_members[chain[0]] if len(chain) == 1 else _combine(group_members, chain) for chain in chains)
    return out
