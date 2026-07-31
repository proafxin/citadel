import logging
import time
from collections import Counter

from citadel.llm import (
    collect_structure_candidates,
    emit_structure_candidates,
)
from citadel.schemas.table import Crosstab, Dimension, KeyColumn, TableStructure
from citadel.tabular.flag import column_kinds, payload_rows
from citadel.tabular.materialize import MaterializedTable, materialize

logger = logging.getLogger(__name__)

_MAX_CELL = 40  # a shown cell is truncated here — the model needs the shape and the label, not a whole paragraph


def _row_line(index: int, row: list[str], width: int) -> str:
    cells = [(row[col] if col < len(row) else "").strip()[:_MAX_CELL] for col in range(width)]
    return f"row {index}: " + " | ".join(cells)


def _payload_text(grid: list[list[str]], indices: list[int], width: int) -> str:
    return "\n".join(_row_line(index, grid[index], width) for index in indices)


SPARSE_HEADER_MIN_WIDTH = 3  # below this a one-cell row may genuinely be the header of a narrow table
SPARSE_HEADER_MAX_CELLS = 1


def _is_header_row(grid: list[list[str]], row: int, width: int) -> bool:
    # a header NAMES THE COLUMNS, so it populates them. a row carrying a single label with the rest empty is a section
    # label or a banner — "Assets" sitting between a balance sheet's header and its rows — however the model labelled
    # it. this is decidable from the grid, so it is not left to the model: it sits directly under the header, which is
    # exactly where a second header level would sit, and the model reads it as one. a rejected row is not dropped —
    # data_start is derived from the header rows that survive, so it simply becomes the data row it always was
    if not 0 <= row < len(grid):
        return False
    populated = sum(1 for cell in grid[row] if cell.strip())
    return not (width >= SPARSE_HEADER_MIN_WIDTH and populated <= SPARSE_HEADER_MAX_CELLS)


def _crosstab(table: dict, width: int) -> Crosstab | None:
    spec = table.get("crosstab")
    if not isinstance(spec, dict):
        return None
    keys = [
        KeyColumn(name=str(k["name"]), col=int(k["col"]))
        for k in spec.get("key_columns", [])
        if isinstance(k, dict) and 0 <= int(k.get("col", -1)) < width
    ]
    dims = [
        Dimension(name=str(d["name"]), header_row=int(d["header_row"]))
        for d in spec.get("dimensions", [])
        if isinstance(d, dict) and int(d.get("header_row", -1)) >= 0
    ]
    start = max(0, min(int(spec.get("value_col_start", 0)), width - 1))
    end = max(start, min(int(spec.get("value_col_end", width - 1)), width - 1))
    if not dims:  # a crosstab with no dimensions is just a relational table the model mislabelled
        return None
    return Crosstab(
        key_columns=keys,
        dimensions=dims,
        value_name=str(spec.get("value_name") or "value"),
        value_col_start=start,
        value_col_end=end,
    )


def stack_candidates(grids: list[list[list[str]]]) -> list[list[str]]:
    # every candidate table — from any file type — is a row/column table. stacking them into one, padded to the widest,
    # is the single input this stage sees: the model reads all of them at once and re-derives table boundaries from the
    # rows themselves, so a table split across regions or pages comes back as continuous rows under one header
    width = max((len(row) for grid in grids for row in grid), default=0)
    return [[row[index] if index < len(row) else "" for index in range(width)] for grid in grids for row in grid]


def _candidate_text(grid: list[list[str]], index: int) -> str:
    width = max((len(row) for row in grid), default=0)
    kinds = column_kinds(grid)
    hint = ", ".join(f"col{col}:{kinds[col]}" for col in range(width))
    body = _payload_text(grid, payload_rows(grid), width)
    return f"{index}: {len(grid)} rows, {width} cols; column kinds: {hint}\n{body}"


def _table_from_spec(spec: dict, grid: list[list[str]]) -> TableStructure:
    # one table the model reported, over its (possibly merged) grid. no boundaries decided here — the model gave them
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
    header_rows = sorted(h for h in spec.get("header_rows", []) if 0 <= h < height and _is_header_row(grid, h, width))
    data_start = (max(header_rows) + 1) if header_rows else 0
    data_end = max(data_start, min(int(spec.get("row_end", height - 1)), max(height - 1, 0)))
    columns = ([str(name) for name in spec.get("columns", [])] or None) if header_rows else None
    crosstab = _crosstab(spec, width) if spec.get("layout") == "crosstab" else None
    section_rows = sorted(
        row for row in spec.get("section_rows", []) if isinstance(row, int) and data_start <= row <= data_end
    )
    return TableStructure(
        layout="crosstab" if crosstab is not None else "relational",
        col_start=col_start,
        col_end=col_end,
        header_rows=header_rows,
        data_start=data_start,
        data_end=data_end,
        columns=columns,
        section_rows=section_rows or None,
        crosstab=crosstab,
        title=spec.get("title") or None,
        notes=spec.get("notes") or [],
    )


def _contained(inner: TableStructure, outer: TableStructure) -> bool:
    return (
        outer.data_start <= inner.data_start
        and inner.data_end <= outer.data_end
        and (inner.data_start, inner.data_end) != (outer.data_start, outer.data_end)
    )


def _drop_contained_specs(
    prepared: list[tuple[list[int], list[list[str]], TableStructure]],
) -> list[tuple[list[int], list[list[str]], TableStructure]]:
    # two specs can share a block without the model coordinating their row ranges against each other — nothing
    # upstream re-checks it. a range strictly inside another one from the SAME block is the same rows claimed twice;
    # keep the larger, more complete table and drop the one it subsumes
    dropped: set[int] = set()
    for i, (blocks_i, _, structure_i) in enumerate(prepared):
        for j, (blocks_j, _, structure_j) in enumerate(prepared):
            if i != j and blocks_i == blocks_j and _contained(structure_i, structure_j):
                dropped.add(i)
    return [item for index, item in enumerate(prepared) if index not in dropped]


async def structure_tables(
    candidates: list[list[list[str]]], *, sheet_no: int = 0, anchors: dict | None = None
) -> list[tuple[MaterializedTable, list[int]]]:
    # THE unified stage — one call per document/sheet, for candidate tables from ANY source. the model sees every
    # candidate labelled, returns the real tables (dropping non-tables) with each table's source candidate INDEX(es) —
    # several = one table split apart, which we concatenate; one block claimed by several tables = one block holding
    # several tables stacked by row, bounded by row_end — and its structure. returns each table with its source
    # candidate indices so the caller can place it (e.g. under its first block). no structure decided in code
    if not candidates:
        return []
    started = time.perf_counter()
    payload = "\n\n".join(_candidate_text(grid, index) for index, grid in enumerate(candidates))
    specs = await collect_structure_candidates(await emit_structure_candidates(payload))
    prepared: list[tuple[list[int], list[list[str]], TableStructure]] = []
    for spec in specs:
        blocks = [index for index in spec.get("blocks", []) if isinstance(index, int) and 0 <= index < len(candidates)]
        if not blocks:
            logger.info("table_structure dropped spec blocks=%r — no valid block index", spec.get("blocks"))
            continue
        grid = candidates[blocks[0]] if len(blocks) == 1 else stack_candidates([candidates[i] for i in blocks])
        prepared.append((blocks, grid, _table_from_spec(spec, grid)))
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
        "table_structure candidates=%d tables=%d merged=%d split=%d secs=%.1f",
        len(candidates),
        len(out),
        merged,
        split,
        time.perf_counter() - started,
    )
    return out
