import asyncio
import logging
import re
import time
from collections import Counter

from citadel.llm import (
    SLM_MODEL_LEN,
    STRUCTURE_MAX_TOKENS,
    call_structure_candidates,
    call_structure_single,
    count_tokens_batch,
    pack_indices,
)
from citadel.services.capacity import get_text_capacity, get_text_large_capacity
from citadel.services.tabular import grid_from_markdown, single_table_structure
from citadel.tabular.flag import SAMPLE_BOTTOM, SAMPLE_TOP, column_kinds, payload_rows
from citadel.tabular.materialize import MaterializedTable, materialize

logger = logging.getLogger(__name__)

_MAX_CELL = 40
STRUCTURE_PAYLOAD_BUDGET = 3800
SINGLE_TABLE_BUDGET = SLM_MODEL_LEN - STRUCTURE_MAX_TOKENS - 2048

_BLOCKS_TAG = re.compile(r"^###\s*blocks:\s*([\d,\s]+)", re.IGNORECASE)
_TITLE_TAG = re.compile(r"^###\s*title:\s*(.+)$", re.IGNORECASE)
_NOTES_TAG = re.compile(r"^###\s*notes:\s*(.+)$", re.IGNORECASE)


def _row_line(index: int, row: list[str], width: int, max_cell: int | None) -> str:
    cells = [(row[col] if col < len(row) else "").strip() for col in range(width)]
    if max_cell is not None:
        cells = [cell[:max_cell] for cell in cells]
    return f"row {index}: " + " | ".join(cells)


def _payload_text(grid: list[list[str]], indices: list[int], width: int, max_cell: int | None) -> str:
    return "\n".join(_row_line(index, grid[index], width, max_cell) for index in indices)


def _spread(rows: list[int], room: int) -> list[int]:
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
    room = max(int(budget / avg), SAMPLE_TOP + SAMPLE_BOTTOM)
    edges = set(rows[:SAMPLE_TOP]) | set(rows[len(rows) - SAMPLE_BOTTOM :])
    middle = [index for index in rows[SAMPLE_TOP : len(rows) - SAMPLE_BOTTOM] if index not in edges]
    return sorted(edges | set(_spread(middle, max(room - len(edges), 0))))


def _candidate_text(
    grid: list[list[str]],
    index: int,
    adjacent: str = "",
    header_hint: list[int] | None = None,
    *,
    full: bool = False,
    budget: int | None = None,
) -> str:
    width = max((len(row) for row in grid), default=0)
    kinds = column_kinds(grid)
    hint = ", ".join(f"col{col}:{kinds[col]}" for col in range(width))
    if full:
        rows = list(range(len(grid)))
    elif budget is not None:
        rows = _budgeted_sample(grid, width, budget)
    else:
        rows = payload_rows(grid)
    body = _payload_text(grid, rows, width, None if full else _MAX_CELL)
    context = f"\n{adjacent}" if adjacent else ""
    header_line = ""
    if header_hint:
        rows_desc = ", ".join(str(row) for row in header_hint)
        header_line = f"\nsource marks row(s) {rows_desc} as header"
    return f"{index}: {len(grid)} rows, {width} cols; column kinds: {hint}{context}{header_line}\n{body}"


def _parse_sections(text: str) -> list[tuple[list[int], str | None, list[str], str]]:
    sections: list[tuple[list[int], str | None, list[str], str]] = []
    blocks: list[int] = []
    title: str | None = None
    notes: list[str] = []
    body: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        blocks_match = _BLOCKS_TAG.match(stripped)
        if blocks_match:
            if blocks and body:
                sections.append((blocks, title, notes, "\n".join(body)))
            blocks = [int(part) for part in blocks_match.group(1).split(",") if part.strip().isdigit()]
            title = None
            notes = []
            body = []
            continue
        title_match = _TITLE_TAG.match(stripped)
        if title_match:
            title = title_match.group(1).strip()
            continue
        notes_match = _NOTES_TAG.match(stripped)
        if notes_match:
            notes = [note.strip() for note in notes_match.group(1).split(";") if note.strip()]
            continue
        body.append(line)
    if blocks and body:
        sections.append((blocks, title, notes, "\n".join(body)))
    return sections


def _grid_structure(grid: list[list[str]], title: str | None, notes: list[str]):
    has_header = any(cell.strip() for cell in grid[0])
    structure = single_table_structure(grid, header_rows=1 if has_header else 0)
    if title or notes:
        structure = structure.model_copy(update={"title": title, "notes": notes or None})
    return structure


def _parse_single(text: str) -> tuple[str | None, list[str], str]:
    title: str | None = None
    notes: list[str] = []
    body: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        title_match = _TITLE_TAG.match(stripped)
        if title_match and not body:
            title = title_match.group(1).strip()
            continue
        notes_match = _NOTES_TAG.match(stripped)
        if notes_match and not body:
            notes = [note.strip() for note in notes_match.group(1).split(";") if note.strip()]
            continue
        body.append(line)
    return title, notes, "\n".join(body)


async def structure_single_table(grid: list[list[str]], *, key: str, full: bool = False) -> MaterializedTable:
    text = _candidate_text(grid, 0, full=full, budget=None if full else SINGLE_TABLE_BUDGET)
    cap = get_text_capacity()
    await cap.acquire(key)
    try:
        raw = await call_structure_single(text)
    finally:
        await cap.release(key)
    title, notes, body = _parse_single(raw)
    parsed = grid_from_markdown(body) or grid
    structure = _grid_structure(parsed, title, notes)
    return materialize(parsed, structure)


async def _collect_sections(
    texts: list[str], packs: list[list[int]], prompt_name: str, sheet_no: int, label: str
) -> list[tuple[list[int], str | None, list[str], str]]:
    sections: list[tuple[list[int], str | None, list[str], str]] = []
    cap = get_text_large_capacity()
    for pack_no, pack in enumerate(packs):
        payload = "\n\n".join(texts[index] for index in pack)
        key = f"structure:{label or prompt_name}:{sheet_no}:{pack_no}"
        await cap.acquire(key)
        try:
            raw = await call_structure_candidates(payload, prompt_name)
        finally:
            await cap.release(key)
        sections.extend(_parse_sections(raw))
    return sections


async def structure_tables(
    candidates: list[list[list[str]]],
    *,
    prompt_name: str,
    sheet_no: int = 0,
    anchors: dict | None = None,
    label: str = "",
    adjacent: list[str] | None = None,
    header_hints: list[list[int]] | None = None,
) -> list[tuple[MaterializedTable, list[int]]]:
    if not candidates:
        return []
    started = time.perf_counter()
    context = adjacent or ["" for _ in candidates]
    hints = header_hints or [[] for _ in candidates]
    texts = [_candidate_text(grid, index, context[index], hints[index]) for index, grid in enumerate(candidates)]
    counts = await asyncio.to_thread(count_tokens_batch, texts)
    packs = pack_indices(counts, STRUCTURE_PAYLOAD_BUDGET)
    sections = await _collect_sections(texts, packs, prompt_name, sheet_no, label)
    out: list[tuple[MaterializedTable, list[int]]] = []
    merged = 0
    block_uses: Counter[int] = Counter()
    for blocks, title, notes, body in sections:
        valid_blocks = [index for index in blocks if 0 <= index < len(candidates)]
        if not valid_blocks:
            logger.info("table_structure dropped blocks=%r — no valid block index", blocks)
            continue
        grid = grid_from_markdown(body)
        if not grid:
            logger.info("table_structure dropped blocks=%s — empty markdown table", valid_blocks)
            continue
        structure = _grid_structure(grid, title, notes)
        table = materialize(grid, structure, sheet_no=sheet_no, anchors=anchors)
        if table.n_rows:
            out.append((table, valid_blocks))
            merged += len(valid_blocks) > 1
            block_uses.update(valid_blocks)
        else:
            logger.info("table_structure dropped blocks=%s — materialized 0 rows", valid_blocks)
    split = sum(1 for count in block_uses.values() if count > 1)
    logger.info(
        "table_structure%s candidates=%d packs=%d tables=%d merged=%d split=%d secs=%.1f",
        f" doc={label}" if label else "",
        len(candidates),
        len(packs),
        len(out),
        merged,
        split,
        time.perf_counter() - started,
    )
    return out
