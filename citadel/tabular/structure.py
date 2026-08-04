import logging
import re
import time
from collections import Counter, defaultdict

from citadel.llm import (
    collect_structure_candidates,
    emit_structure_candidates,
)
from citadel.schemas.table import Crosstab, Dimension, KeyColumn, TableStructure
from citadel.tabular.flag import column_kinds, payload_rows
from citadel.tabular.materialize import MaterializedTable, _plausible_header_rows, materialize

logger = logging.getLogger(__name__)

_MAX_CELL = 40  # a shown cell is truncated here — the model needs the shape and the label, not a whole paragraph
_MIN_SEQUENCE_RUN = 3  # below this, any two numbers trivially "form a sequence" by definition — not a real signal


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


def _numeric_value(cell: str) -> float | None:
    text = cell.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _sequence_hint_rows(grid: list[list[str]], width: int) -> list[int]:
    # a run of adjacent cells stepping by a constant difference (1, 2, 3, 4 or 1990, 1991, 1992, ...) is how a wide
    # table spells out a dimension across columns instead of naming it once — years, indices, buckets. the row
    # holding it is a header for that dimension (see crosstab, in the prompt), not a data row, even though every one
    # of its cells is a plain number and would otherwise read exactly like the data beneath it
    hints: list[int] = []
    for row_index, row in enumerate(grid):
        longest = run_length = 0
        run_step: float | None = None
        last: float | None = None
        for col in range(width):
            value = _numeric_value(row[col]) if col < len(row) else None
            if value is None:
                run_length, run_step, last = 0, None, None
                continue
            if last is None:
                run_length, run_step = 1, None
            elif run_step == value - last:
                run_length += 1
            else:
                run_length, run_step = 2, value - last
            last = value
            longest = max(longest, run_length)
        if longest >= _MIN_SEQUENCE_RUN:
            hints.append(row_index)
    return hints


def _candidate_text(
    grid: list[list[str]], index: int, header_hint: list[int] | None = None, title_hint: list[int] | None = None
) -> str:
    width = max((len(row) for row in grid), default=0)
    kinds = column_kinds(grid)
    hint = ", ".join(f"col{col}:{kinds[col]}" for col in range(width))
    header_line = ""
    if header_hint:
        rows = ", ".join(str(row) for row in header_hint)
        header_line = f"; row(s) {rows} came from the source already marked as a header"
    title_line = ""
    if title_hint:
        rows = ", ".join(str(row) for row in title_hint)
        title_line = f"; row(s) {rows} are a lone label in the source (bold, parenthetical, or footnote), not data"
    sequence_hint = _sequence_hint_rows(grid, width)
    sequence_line = ""
    if sequence_hint:
        rows = ", ".join(str(row) for row in sequence_hint)
        sequence_line = f"; row(s) {rows} step in a steady numeric sequence across their columns"
    body = _payload_text(grid, payload_rows(grid), width)
    return (
        f"{index}: {len(grid)} rows, {width} cols; column kinds: {hint}{header_line}{title_line}{sequence_line}\n{body}"
    )


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
    title = spec.get("title") or None
    data_start = (max(header_rows) + 1) if header_rows else 0
    if title and data_start == 0 and 0 not in header_rows:
        # a title names the whole table and is never one of its rows, so it is excluded from data the same way a
        # header row is — even for a label-value block, where header_rows stays empty by design (the carve-out
        # above) and `title` is what signals the exclusion instead. its own row is taken to be row 0 of the block,
        # the same place a lone label naming the whole block is expected to sit everywhere else in this stage
        data_start = 1
    # applied here, not only in materialize._materialize_relational, so _drop_contained_specs (which runs on this
    # data_start/data_end, before materialize is ever called) sees the same corrected range that will actually be
    # produced — otherwise a rejected header row's pull-back is invisible to containment detection and a spec that
    # only looks non-overlapping on paper survives as a duplicate of the rows the correction folds into another spec
    plausible_rows = _plausible_header_rows(grid, header_rows, width)
    rejected_rows = set(header_rows) - set(plausible_rows)
    if rejected_rows:
        data_start = min(data_start, *rejected_rows)
    header_rows = plausible_rows
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
    # data_start/data_end are row indices into the grid stack_candidates builds by concatenating these blocks IN
    # ORDER — comparable between two specs only when one block list is a leading prefix of the other, so index 0
    # means the same physical starting row for both (an identical list is trivially a prefix of itself, so this
    # covers the same-block case too). any other pairing stacks a different set of rows in a different order, and
    # their indices mean nothing next to each other
    shorter, longer = (blocks_i, blocks_j) if len(blocks_i) <= len(blocks_j) else (blocks_j, blocks_i)
    return longer[: len(shorter)] == shorter


def _drop_contained_specs(
    prepared: list[tuple[list[int], list[list[str]], TableStructure]],
) -> list[tuple[list[int], list[list[str]], TableStructure]]:
    # two specs can share rows without the model coordinating their ranges against each other — nothing upstream
    # re-checks it, and this is not limited to specs sharing the identical block list: a spec covering just the
    # block a bigger spec starts with claims the same rows just as much as an exact block match would. a range
    # strictly inside another one, from block lists whose indices are comparable, is the same rows claimed twice;
    # keep the larger, more complete table and drop the one it subsumes
    dropped: set[int] = set()
    for i, (blocks_i, _, structure_i) in enumerate(prepared):
        for j, (blocks_j, _, structure_j) in enumerate(prepared):
            if i != j and _index_comparable(blocks_i, blocks_j) and _contained(structure_i, structure_j):
                dropped.add(i)
    return [item for index, item in enumerate(prepared) if index not in dropped]


_SHORT_INDEX = re.compile(r"^\d{1,4}\.?$")  # a bare row-number ("1", "23.") — never prose, so it marks a blank
# form's own placeholder row rather than a caption the model mistook for data


def _is_degenerate(table: MaterializedTable) -> bool:
    # a table that resolves to one row with only one cell filled is almost always a caption or banner the model
    # mistook for data — the same sparse signal that already disqualifies a HEADER row (SPARSE_HEADER_MIN_WIDTH/
    # MAX_CELLS), applied here to whether the table should exist at all. the one legitimate exception is a blank
    # form's own row-index placeholder: that lone cell is a short digit token, never prose, so it is exempted
    # rather than dropped along with genuine captions
    if table.n_rows != 1 or len(table.columns) < SPARSE_HEADER_MIN_WIDTH:
        return False
    populated = [cell for cell in table.sample_rows[0] if cell not in {None, ""}]
    if len(populated) > SPARSE_HEADER_MAX_CELLS:
        return False
    return not populated or not _SHORT_INDEX.match(str(populated[0]).strip())


def _spec_covered_rows(blocks: list[int], structure: TableStructure) -> tuple[int, set[int]] | None:
    # multi-block specs index into stack_candidates' concatenated grid, not this block's own row numbers, so
    # gap-checking is scoped to single-block specs — the overwhelming majority, and the ones every observed
    # omission so far has involved
    if len(blocks) != 1:
        return None
    rows = set(range(structure.data_start, structure.data_end + 1))
    rows.update(structure.header_rows or [])
    if structure.title:
        rows.add(0)
    return blocks[0], rows


def _log_gaps(candidates: list[list[list[str]]], covered: dict[int, set[int]]) -> None:
    # nothing corrects this automatically — there is no spec for an unclaimed row to fix. logged so a row the model
    # silently never accounted for is visible instead of quietly vanishing from the ingested table
    for index, grid in enumerate(candidates):
        if index not in covered:
            continue
        gap = {row for row, cells in enumerate(grid) if any(cell.strip() for cell in cells)} - covered[index]
        if gap:
            logger.info("table_structure gap block=%d unclaimed_rows=%s", index, sorted(gap))


async def structure_tables(
    candidates: list[list[list[str]]],
    *,
    sheet_no: int = 0,
    anchors: dict | None = None,
    header_hints: list[list[int]] | None = None,
    title_hints: list[list[int]] | None = None,
    label: str = "",
) -> list[tuple[MaterializedTable, list[int]]]:
    # THE unified stage — one call per document/sheet, for candidate tables from ANY source. the model sees every
    # candidate labelled, returns the real tables (dropping non-tables) with each table's source candidate INDEX(es) —
    # several = one table split apart, which we concatenate; one block claimed by several tables = one block holding
    # several tables stacked by row, bounded by row_end — and its structure. returns each table with its source
    # candidate indices so the caller can place it (e.g. under its first block). no structure decided in code.
    # header_hints, when given, is one row-index list per candidate: rows the SOURCE format already marked as a
    # header (a table's own <th>/OTSL <ched> row) — a real signal, not a guess, so it rides along as a hint rather
    # than being decided here; the model still makes the call, same as everything else in this stage. title_hints is
    # the same idea for a lone bold, otherwise-empty row — a source-format signal a row is a title/section label.
    # label, when given, identifies the caller in the summary log line only — it plays no role in structuring
    if not candidates:
        return []
    started = time.perf_counter()
    header = header_hints or [[] for _ in candidates]
    title = title_hints or [[] for _ in candidates]
    payload = "\n\n".join(
        _candidate_text(grid, index, header[index], title[index]) for index, grid in enumerate(candidates)
    )
    specs = await collect_structure_candidates(await emit_structure_candidates(payload))
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
    covered: dict[int, set[int]] = defaultdict(set)
    for blocks, grid, structure in _drop_contained_specs(prepared):
        table = materialize(grid, structure, sheet_no=sheet_no, anchors=anchors)
        if table.n_rows and not _is_degenerate(table):
            out.append((table, blocks))
            merged += len(blocks) > 1
            block_uses.update(blocks)
            if covered_rows := _spec_covered_rows(blocks, structure):
                index, rows = covered_rows
                covered[index] |= rows
        else:
            reason = "materialized 0 rows" if not table.n_rows else "degenerate single-cell row"
            logger.info("table_structure dropped blocks=%s — %s", blocks, reason)
    _log_gaps(candidates, covered)
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
