import logging
import time
from collections import Counter

from citadel.llm import (
    collect_structure_candidates,
    emit_structure_candidates,
)
from citadel.schemas.table import TableStructure
from citadel.tabular.flag import column_kinds, payload_rows
from citadel.tabular.materialize import MaterializedTable, materialize

logger = logging.getLogger(__name__)

_MAX_CELL = 40


def _row_line(index: int, row: list[str], width: int) -> str:
    cells = [(row[col] if col < len(row) else "").strip()[:_MAX_CELL] for col in range(width)]
    return f"row {index}: " + " | ".join(cells)


def _payload_text(grid: list[list[str]], indices: list[int], width: int) -> str:
    return "\n".join(_row_line(index, grid[index], width) for index in indices)


def stack_candidates(grids: list[list[list[str]]]) -> list[list[str]]:
    width = max((len(row) for grid in grids for row in grid), default=0)
    return [[row[index] if index < len(row) else "" for index in range(width)] for grid in grids for row in grid]


_MAX_ADJACENT = 200


def _candidate_text(grid: list[list[str]], index: int, adjacent: str = "") -> str:
    width = max((len(row) for row in grid), default=0)
    kinds = column_kinds(grid)
    hint = ", ".join(f"col{col}:{kinds[col]}" for col in range(width))
    body = _payload_text(grid, payload_rows(grid), width)
    context = f"\n{adjacent[:_MAX_ADJACENT]}" if adjacent else ""
    return f"{index}: {len(grid)} rows, {width} cols; column kinds: {hint}{context}\n{body}"


def _table_from_spec(spec: dict, grid: list[list[str]]) -> TableStructure:
    height = len(grid)
    width = max((len(row) for row in grid), default=0)
    col_start = max(0, min(int(spec.get("col_start", 0)), max(width - 1, 0)))
    col_end = max(col_start, min(int(spec.get("col_end", width - 1)), max(width - 1, 0)))
    if bool(spec.get("transposed")):
        data_end = max(0, min(int(spec.get("row_end", height - 1)), max(height - 1, 0)))
        return TableStructure(
            transposed=True,
            col_start=col_start,
            col_end=col_end,
            header_rows=[],
            data_start=0,
            data_end=data_end,
            title=spec.get("title") or None,
            notes=spec.get("notes") or [],
        )
    header_rows = sorted(h for h in spec.get("header_rows", []) if 0 <= h < height)
    title = spec.get("title") or None
    data_start = (max(header_rows) + 1) if header_rows else 0
    if title and data_start == 0 and 0 not in header_rows:
        data_start = 1
    data_end = max(data_start, min(int(spec.get("row_end", height - 1)), max(height - 1, 0)))
    columns = ([str(name) for name in spec.get("columns", [])] or None) if header_rows else None
    section_rows = sorted(
        row for row in spec.get("section_rows", []) if isinstance(row, int) and data_start <= row <= data_end
    )
    return TableStructure(
        col_start=col_start,
        col_end=col_end,
        header_rows=header_rows,
        data_start=data_start,
        data_end=data_end,
        columns=columns,
        section_rows=section_rows or None,
        title=title,
        notes=spec.get("notes") or [],
    )


def _contained(inner: TableStructure, outer: TableStructure) -> bool:
    return (
        outer.data_start <= inner.data_start
        and inner.data_end <= outer.data_end
        and (inner.data_start, inner.data_end) != (outer.data_start, outer.data_end)
    )


def _index_comparable(blocks_i: list[int], blocks_j: list[int]) -> bool:
    shorter, longer = (blocks_i, blocks_j) if len(blocks_i) <= len(blocks_j) else (blocks_j, blocks_i)
    return longer[: len(shorter)] == shorter


def _drop_contained_specs(
    prepared: list[tuple[list[int], list[list[str]], TableStructure]],
) -> list[tuple[list[int], list[list[str]], TableStructure]]:
    dropped: set[int] = set()
    for i, (blocks_i, _, structure_i) in enumerate(prepared):
        for j, (blocks_j, _, structure_j) in enumerate(prepared):
            if i != j and _index_comparable(blocks_i, blocks_j) and _contained(structure_i, structure_j):
                dropped.add(i)
    return [item for index, item in enumerate(prepared) if index not in dropped]


async def structure_tables(
    candidates: list[list[list[str]]],
    *,
    prompt_name: str,
    sheet_no: int = 0,
    anchors: dict | None = None,
    label: str = "",
    adjacent: list[str] | None = None,
) -> list[tuple[MaterializedTable, list[int]]]:
    if not candidates:
        return []
    started = time.perf_counter()
    context = adjacent or ["" for _ in candidates]
    payload = "\n\n".join(_candidate_text(grid, index, context[index]) for index, grid in enumerate(candidates))
    specs = await collect_structure_candidates(await emit_structure_candidates(payload, prompt_name))
    prepared: list[tuple[list[int], list[list[str]], TableStructure]] = []
    for spec in specs:
        blocks = [index for index in spec.get("blocks", []) if isinstance(index, int) and 0 <= index < len(candidates)]
        if not blocks:
            logger.info("table_structure dropped spec blocks=%r — no valid block index", spec.get("blocks"))
            continue
        grid = candidates[blocks[0]] if len(blocks) == 1 else stack_candidates([candidates[i] for i in blocks])
        structure = _table_from_spec(spec, grid)
        logger.info(
            "table_structure spec blocks=%s data=%d-%d header_rows=%s title=%r",
            blocks,
            structure.data_start,
            structure.data_end,
            structure.header_rows,
            structure.title,
        )
        prepared.append((blocks, grid, structure))
    out: list[tuple[MaterializedTable, list[int]]] = []
    merged = 0
    block_uses: Counter[int] = Counter()
    for blocks, grid, structure in _drop_contained_specs(prepared):
        table = materialize(grid, structure, sheet_no=sheet_no, anchors=anchors)
        if table.n_rows:
            out.append((table, blocks))
            merged += len(blocks) > 1
            block_uses.update(blocks)
        else:
            logger.info("table_structure dropped blocks=%s — materialized 0 rows", blocks)
    split = sum(1 for count in block_uses.values() if count > 1)
    logger.info(
        "table_structure%s candidates=%d tables=%d merged=%d split=%d secs=%.1f",
        f" doc={label}" if label else "",
        len(candidates),
        len(out),
        merged,
        split,
        time.perf_counter() - started,
    )
    return out
