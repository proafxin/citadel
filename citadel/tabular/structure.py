import operator

from citadel.llm import structure_sheet
from citadel.schemas.table import TableStructure
from citadel.tabular.flag import classify_rows, column_kinds, payload_rows

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
        out.append(
            TableStructure(
                col_start=col_start,
                col_end=col_end,
                header_rows=header_rows,
                data_start=data_start,
                data_end=data_end,
                title=table.get("title") or None,
                notes=table.get("notes") or [],
                description=table.get("description") or None,
            )
        )
    return out


def _fallback(grid: list[list[str]], height: int, width: int) -> list[TableStructure]:
    # used only when the model returns nothing usable: the leading run of interesting rows is the header, the rest is
    # data. deterministic and never empty, so a table always materializes rather than being lost
    labels = classify_rows(grid)
    header_rows: list[int] = []
    for index, label in enumerate(labels):
        if label != "interesting":
            break
        header_rows.append(index)
    data_start = len(header_rows)
    if data_start >= height:
        header_rows, data_start = [], 0
    return [
        TableStructure(
            col_start=0, col_end=width - 1, header_rows=header_rows, data_start=data_start, data_end=height - 1
        )
    ]


async def structure_grid(grid: list[list[str]]) -> list[TableStructure]:
    height = len(grid)
    if height == 0:
        return []
    width = max(len(row) for row in grid)
    indices = payload_rows(grid)
    kinds = column_kinds(grid)
    hint = ", ".join(f"col{col}:{kinds[col]}" for col in range(width))
    tables = await structure_sheet(_payload_text(grid, indices, width), hint, height, width)
    derived = _derive(tables, grid, height, width)
    return derived or _fallback(grid, height, width)
