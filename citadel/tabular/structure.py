import asyncio
import logging
from itertools import starmap
from typing import Protocol

from citadel.llm import call_structured, count_tokens, count_tokens_batch
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

_TABLE_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "header_rows": {"type": "array", "items": {"type": "integer"}},
        "transposed": {"type": "boolean"},
        "data_start": {"type": "integer"},
        "data_end": {"type": "integer"},
        "col_start": {"type": "integer"},
        "col_end": {"type": "integer"},
        "section_rows": {"type": "array", "items": {"type": "integer"}},
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
        "section_rows",
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


_SAMPLE_RUN = 3


def _spread(rows: list[int], room: int) -> list[int]:
    if room <= 0 or not rows:
        return []
    if room >= len(rows):
        return rows
    runs = max(room // _SAMPLE_RUN, 1)
    step = len(rows) / runs
    selected: set[int] = set()
    for index in range(runs):
        start = int(index * step)
        selected.update(rows[start : start + _SAMPLE_RUN])
    return sorted(selected)


def _budgeted_sample(grid: list[list[str]], width: int, budget: int) -> list[int]:
    rows = list(range(len(grid)))
    if not rows:
        return []
    lines = [_row_line(index, grid[index], width, _MAX_CELL) for index in rows]
    counts = count_tokens_batch(lines)
    if sum(counts) <= budget:
        return rows
    avg = sum(counts) / len(counts)
    room = max(int(budget / avg), SAMPLE_TOP + SAMPLE_BOTTOM)
    edges = set(rows[:SAMPLE_TOP]) | set(rows[len(rows) - SAMPLE_BOTTOM :])
    middle = [index for index in rows[SAMPLE_TOP : len(rows) - SAMPLE_BOTTOM] if index not in edges]
    return sorted(edges | set(_spread(middle, max(room - len(edges), 0))))


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


def _structure(entry: dict) -> TableStructure:
    col_start, col_end = _entry_col_range(entry)
    return TableStructure(
        col_start=col_start,
        col_end=col_end,
        header_rows=list(entry.get("header_rows") or []) or None,
        transposed=bool(entry.get("transposed")),
        data_start=int(entry["data_start"]),
        data_end=int(entry["data_end"]),
        section_rows=list(entry.get("section_rows") or []) or None,
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
) -> list[MaterializedTable]:
    if not grid:
        return []
    text = _candidate_text(grid, full=full, budget=None if full else SINGLE_TABLE_BUDGET)
    prompt_name = "table_structure_known" if known_table else "table_structure_excel"
    prompt = f"{load_prompt(prompt_name)}\n{text}"
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
