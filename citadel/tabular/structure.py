from citadel.llm import structure_sheet
from citadel.schemas.table import TableStructure
from citadel.tabular.flag import classify_rows, column_kinds, payload_rows

_MAX_CELL = 40  # a shown cell is truncated here — the model needs the shape and the label, not a whole paragraph


def _row_line(index: int, row: list[str], width: int) -> str:
    cells = [(row[col] if col < len(row) else "").strip()[:_MAX_CELL] for col in range(width)]
    return f"row {index}: " + " | ".join(cells)


def _payload_text(grid: list[list[str]], indices: list[int], width: int) -> str:
    return "\n".join(_row_line(index, grid[index], width) for index in indices)


def _derive(tables: list[dict], height: int, width: int) -> list[TableStructure]:
    # the model returns header rows + column span per table; the data spans are ours to compute. each table owns the
    # rows from just after its header down to just before the next table starts (or the sheet's end)
    ordered = sorted(
        ({"raw": table, "top": min([*table.get("header_rows", []), height])} for table in tables),
        key=lambda item: item["top"],
    )
    out: list[TableStructure] = []
    for position, item in enumerate(ordered):
        table = item["raw"]
        header_rows = sorted(h for h in table.get("header_rows", []) if 0 <= h < height)
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
    derived = _derive(tables, height, width)
    return derived or _fallback(grid, height, width)
