import logging
import time
from collections import Counter
from collections.abc import Callable

from citadel.llm import (
    STRUCTURE_BUDGET_OCR,
    collect_structure_candidates,
    collect_structure_candidates_ocr,
    emit_structure_candidates,
    emit_structure_candidates_ocr,
    structure_prompt_tokens_ocr,
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


def _candidate_text(grid: list[list[str]], index: int, adjacent: str = "", header_hint: list[int] | None = None) -> str:
    width = max((len(row) for row in grid), default=0)
    kinds = column_kinds(grid)
    hint = ", ".join(f"col{col}:{kinds[col]}" for col in range(width))
    body = _payload_text(grid, payload_rows(grid), width)
    context = f"\n{adjacent}" if adjacent else ""
    header_line = ""
    if header_hint:
        rows = ", ".join(str(row) for row in header_hint)
        header_line = f"\nsource marks row(s) {rows} as header"
    return f"{index}: {len(grid)} rows, {width} cols; column kinds: {hint}{context}{header_line}\n{body}"


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
    header_hints: list[list[int]] | None = None,
) -> list[tuple[MaterializedTable, list[int]]]:
    if not candidates:
        return []
    started = time.perf_counter()
    context = adjacent or ["" for _ in candidates]
    hints = header_hints or [[] for _ in candidates]
    payload = "\n\n".join(
        _candidate_text(grid, index, context[index], hints[index]) for index, grid in enumerate(candidates)
    )
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


def _ocr_row_line(index: int, row: list[str], width: int) -> str:
    cells = [(row[col] if col < len(row) else "").strip() for col in range(width)]
    return f"{index}: " + " | ".join(cells)


def _ocr_candidate_text(grid: list[list[str]], index: int) -> str:
    width = max((len(row) for row in grid), default=0)
    body = "\n".join(_ocr_row_line(row_index, row, width) for row_index, row in enumerate(grid))
    return f"Table {index}:\n{body}"


def _row_offsets(ids: list[int], candidates: list[list[list[str]]]) -> dict[int, int]:
    offsets: dict[int, int] = {}
    total = 0
    for candidate_id in ids:
        offsets[candidate_id] = total
        total += len(candidates[candidate_id])
    return offsets


def _resolve_line(ref: object, default_table: int, offsets: dict[int, int]) -> int:
    if isinstance(ref, dict):
        return offsets[int(ref["table"])] + int(ref["line"])
    if isinstance(ref, int):
        return offsets[default_table] + ref
    message = f"invalid line reference: {ref!r}"
    raise TypeError(message)


def _grid_cell(grid: list[list[str]], row: int, col: int) -> str:
    return grid[row][col] if 0 <= row < len(grid) and 0 <= col < len(grid[row]) else ""


def _labeled_columns(entries: list[dict] | None, width: int) -> list[str] | None:
    if not entries:
        return None
    labels = [""] * width
    for entry in entries:
        index = int(entry["index"])
        if 0 <= index < width:
            labels[index] = str(entry["label"])
    return labels


def _ocr_metadata(
    spec: dict, default_table: int, offsets: dict[int, int], grid: list[list[str]]
) -> tuple[str | None, list[str]]:
    title = None
    notes: list[str] = []
    for category, ref in (spec.get("metadata") or {}).items():
        line = _resolve_line(ref, default_table, offsets)
        text = " ".join(cell for cell in grid[line] if cell).strip() if 0 <= line < len(grid) else ""
        if category == "title":
            title = text or None
        elif text:
            notes.append(f"{category}: {text}")
    return title, notes


def _forward_fill(grid: list[list[str]], line: int, start: int, end: int) -> list[str]:
    filled: list[str] = []
    current = ""
    for col in range(start, end + 1):
        cell = _grid_cell(grid, line, col).strip()
        if cell:
            current = cell
        filled.append(current)
    return filled


def _crosstab_grid(
    spec: dict, grid: list[list[str]], rows_start: int, rows_end: int, resolve: Callable[[object], int]
) -> list[list[str]]:
    key_columns = spec.get("key_columns") or []
    dimensions = [{"label": d["label"], "line": resolve(d["line"])} for d in spec.get("dimensions") or []]
    value_start = int(spec["value_start"])
    value_end = int(spec["value_end"])
    filled = [_forward_fill(grid, dimension["line"], value_start, value_end) for dimension in dimensions]
    header = [str(k["label"]) for k in key_columns] + [str(d["label"]) for d in dimensions] + ["value"]
    tidy = [header]
    for row in range(rows_start, rows_end + 1):
        keys = [_grid_cell(grid, row, int(k["index"])) for k in key_columns]
        for offset, col in enumerate(range(value_start, value_end + 1)):
            value = _grid_cell(grid, row, col)
            if not value.strip():
                continue
            dims = [filled[dimension_index][offset] for dimension_index in range(len(dimensions))]
            tidy.append([*keys, *dims, value])
    return tidy


def _crosstab_structure(
    spec: dict,
    stitched: list[list[str]],
    resolve: Callable[[object], int],
    rows_start: int,
    rows_end: int,
    title: str | None,
    notes: list[str],
) -> tuple[list[list[str]], TableStructure]:
    tidy = _crosstab_grid(spec, stitched, rows_start, rows_end, resolve)
    structure = TableStructure(
        col_start=0,
        col_end=max((len(row) for row in tidy), default=1) - 1,
        header_rows=[0],
        data_start=1,
        data_end=len(tidy) - 1,
        title=title,
        notes=notes,
    )
    return tidy, structure


def _transpose_structure(
    spec: dict,
    stitched: list[list[str]],
    resolve: Callable[[object], int],
    width: int,
    rows_end: int,
    title: str | None,
    notes: list[str],
) -> tuple[list[list[str]], TableStructure]:
    data_start = resolve(spec["headers_start"]) if "headers_start" in spec else 0
    structure = TableStructure(
        transposed=True,
        col_start=0,
        col_end=max(width - 1, 0),
        header_rows=[],
        data_start=data_start,
        data_end=rows_end,
        title=title,
        notes=notes,
    )
    return stitched, structure


def _rectangular_structure(
    spec: dict,
    stitched: list[list[str]],
    resolve: Callable[[object], int],
    width: int,
    rows_start: int,
    rows_end: int,
    title: str | None,
    notes: list[str],
) -> tuple[list[list[str]], TableStructure]:
    header_rows: list[int] = []
    if "headers_start" in spec and "headers_end" in spec:
        header_rows = list(range(resolve(spec["headers_start"]), resolve(spec["headers_end"]) + 1))
    columns = _labeled_columns(spec.get("columns"), width)
    section_rows = [resolve(entry["line"]) for entry in spec.get("sections") or []]
    structure = TableStructure(
        col_start=0,
        col_end=max(width - 1, 0),
        header_rows=header_rows,
        data_start=rows_start,
        data_end=rows_end,
        columns=columns,
        section_rows=section_rows or None,
        title=title,
        notes=notes,
    )
    return stitched, structure


def _table_from_ocr_spec(
    spec: dict, ids: list[int], candidates: list[list[list[str]]]
) -> tuple[list[list[str]], TableStructure]:
    stitched = stack_candidates([candidates[i] for i in ids])
    offsets = _row_offsets(ids, candidates)
    default_table = ids[0]
    width = max((len(row) for row in stitched), default=0)

    def resolve(ref: object) -> int:
        return _resolve_line(ref, default_table, offsets)

    rows_start = resolve(spec["rows_start"])
    rows_end = resolve(spec["rows_end"])
    title, notes = _ocr_metadata(spec, default_table, offsets, stitched)
    layout = spec.get("layout", "rectangular")

    if layout == "crosstab":
        return _crosstab_structure(spec, stitched, resolve, rows_start, rows_end, title, notes)
    if layout == "transpose":
        return _transpose_structure(spec, stitched, resolve, width, rows_end, title, notes)
    return _rectangular_structure(spec, stitched, resolve, width, rows_start, rows_end, title, notes)


async def structure_tables_ocr(
    candidates: list[list[list[str]]],
    *,
    label: str = "",
) -> list[tuple[MaterializedTable, list[int]]]:
    if not candidates:
        return []
    started = time.perf_counter()
    payload = "\n\n".join(_ocr_candidate_text(grid, index) for index, grid in enumerate(candidates))
    tokens = structure_prompt_tokens_ocr(payload)
    if tokens > STRUCTURE_BUDGET_OCR:
        logger.warning(
            "table_structure_ocr%s payload=%d tokens exceeds budget=%d — sending anyway, every candidate is kept",
            f" doc={label}" if label else "",
            tokens,
            STRUCTURE_BUDGET_OCR,
        )
    specs = await collect_structure_candidates_ocr(await emit_structure_candidates_ocr(payload))
    out: list[tuple[MaterializedTable, list[int]]] = []
    merged = 0
    valid_ids = set(range(len(candidates)))
    for key, spec in specs.items():
        ids_raw = spec.get("tables") or ([int(key)] if key.isdigit() else [])
        ids = [i for i in ids_raw if isinstance(i, int) and i in valid_ids]
        if not ids:
            logger.info("table_structure_ocr dropped spec key=%r — no valid table id", key)
            continue
        grid, structure = _table_from_ocr_spec(spec, ids, candidates)
        table = materialize(grid, structure)
        if table.n_rows:
            out.append((table, ids))
            merged += len(ids) > 1
        else:
            logger.info("table_structure_ocr dropped ids=%s — materialized 0 rows", ids)
    logger.info(
        "table_structure_ocr%s candidates=%d tables=%d merged=%d secs=%.1f",
        f" doc={label}" if label else "",
        len(candidates),
        len(out),
        merged,
        time.perf_counter() - started,
    )
    return out
