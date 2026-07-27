import logging
import operator

from citadel.llm import collect_structure_sheet, emit_structure_sheet
from citadel.schemas.table import Crosstab, Dimension, KeyColumn, TableStructure
from citadel.tabular.flag import column_kinds, payload_rows

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


def _derive(tables: list[dict], grid: list[list[str]], height: int, width: int) -> list[TableStructure]:
    # the model returns header rows + column span per table; the data spans are ours to compute. each table owns the
    # rows from just after its header down to just before the next table starts (or the sheet's end)
    ordered = sorted(
        ({"raw": table, "top": min([*table.get("header_rows", []), height])} for table in tables),
        key=operator.itemgetter("top"),
    )
    out: list[TableStructure] = []
    for position, item in enumerate(ordered):
        table = item["raw"]
        header_rows = sorted(
            h for h in table.get("header_rows", []) if 0 <= h < height and _is_header_row(grid, h, width)
        )
        col_start = max(0, min(int(table.get("col_start", 0)), width - 1))
        col_end = max(col_start, min(int(table.get("col_end", width - 1)), width - 1))
        data_start = (max(header_rows) + 1) if header_rows else int(item["top"] if item["top"] < height else 0)
        data_end = (int(ordered[position + 1]["top"]) - 1) if position + 1 < len(ordered) else height - 1
        if data_start > data_end:
            continue
        columns = [str(name) for name in table.get("columns", [])] or None
        crosstab = _crosstab(table, width) if table.get("layout") == "crosstab" else None
        out.append(
            TableStructure(
                layout="crosstab" if crosstab is not None else "relational",
                col_start=col_start,
                col_end=col_end,
                header_rows=header_rows,
                data_start=data_start,
                data_end=data_end,
                columns=columns,
                crosstab=crosstab,
                title=table.get("title") or None,
                notes=table.get("notes") or [],
            )
        )
    return out


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


# measurement switch. True skips the SLM entirely so a run measures the pipeline with zero table-structure work.
# it produces NO tables rather than deterministic ones — there is no fallback to fall back to, and inventing a
# structure to fill the gap is exactly what was just removed
SKIP_TABLE_SLM = False


async def emit_structure_grid(grid: list[list[str]]) -> str | None:
    # emit the structure job WITHOUT awaiting it, so a sheet's many regions are all in flight on the slm worker at once
    # rather than one after another. returns the job id (None when there is nothing to ask). the grid prep is CPU and
    # stays here; deriving the answer waits for collect
    if len(grid) == 0 or SKIP_TABLE_SLM:
        return None
    width = max(len(row) for row in grid)
    indices = payload_rows(grid)
    kinds = column_kinds(grid)
    hint = ", ".join(f"col{col}:{kinds[col]}" for col in range(width))
    return await emit_structure_sheet(_payload_text(grid, indices, width), hint, len(grid), width)


def _log_crosstabs(structures: list[TableStructure]) -> None:
    # the crosstab RECIPE is applied then discarded — it is not stored anywhere, unlike the materialized columns/rows
    # which land in `tables`. so log only the recipe; the result is queryable from the db
    for structure in structures:
        if structure.crosstab is not None:
            crosstab = structure.crosstab
            logger.info(
                "crosstab data_rows=%d-%d keys=%s dims=%s value=%r valcols=%d-%d",
                structure.data_start,
                structure.data_end,
                [(key.name, key.col) for key in crosstab.key_columns],
                [(dim.name, dim.header_row) for dim in crosstab.dimensions],
                crosstab.value_name,
                crosstab.value_col_start,
                crosstab.value_col_end,
            )


async def collect_structure_grid(grid: list[list[str]], job_id: str) -> list[TableStructure]:
    tables = await collect_structure_sheet(job_id)
    structures = _derive(tables, grid, len(grid), max(len(row) for row in grid))  # nothing is invented here
    _log_crosstabs(structures)
    return structures


async def structure_grid(grid: list[list[str]]) -> list[TableStructure]:
    job_id = await emit_structure_grid(grid)
    return [] if job_id is None else await collect_structure_grid(grid, job_id)
