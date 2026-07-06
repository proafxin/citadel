import asyncio
import json
import re
from decimal import Decimal
from io import BytesIO

import polars as pl
from bs4 import BeautifulSoup
from bs4.element import Tag

from citadel.schemas.content import Block
from citadel.schemas.table import CellValue, Column, ColumnDType, TableStructure
from citadel.services.excel import SAMPLE_TABLE_ROWS, MaterializedTable
from citadel.tabular.infer import predict_pooled, structure_from_mask

_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+\.\d+")


def read_csv_grid(data: bytes, separator: str) -> list[list[str]]:
    # the verbatim cell grid with NO header assumption (has_header=False) — the model decides which rows are headers.
    # every column Utf8 (infer_schema_length=0) so cells stay exact; ragged lines are padded, blanks become ""
    frame = pl.read_csv(
        BytesIO(data), separator=separator, has_header=False, infer_schema_length=0, truncate_ragged_lines=True
    )
    return [["" if value is None else str(value) for value in row] for row in frame.iter_rows()]


def _table_rows(table: Tag) -> list[Tag]:
    # the table's OWN rows only — direct <tr> plus <tr> inside a direct thead/tbody/tfoot. a <table> nested inside a
    # <td> has its rows deeper, so recursive=False never reaches them → no phantom rows bleeding into the outer grid
    rows: list[Tag] = []
    for child in table.find_all(["tr", "thead", "tbody", "tfoot"], recursive=False):
        if child.name == "tr":
            rows.append(child)
        else:
            rows.extend(child.find_all("tr", recursive=False))
    return rows


def _grid(table: Tag) -> list[list[str]]:
    occupied: dict[tuple[int, int], str] = {}
    width = 0
    height = 0
    for row_idx, tr in enumerate(_table_rows(table)):
        col = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (row_idx, col) in occupied:
                col += 1
            value = cell.get_text(separator=" ", strip=True)
            colspan = max(int(cell.get("colspan") or 1), 1)
            rowspan = max(int(cell.get("rowspan") or 1), 1)
            for delta_row in range(rowspan):
                for delta_col in range(colspan):
                    occupied[row_idx + delta_row, col + delta_col] = value
            col += colspan
            width = max(width, col)
        height = row_idx + 1
    return [[occupied.get((row, col), "") for col in range(width)] for row in range(height)]


def _header_count(table: Tag) -> int:
    thead = table.find("thead", recursive=False)
    if isinstance(thead, Tag):
        return max(len(thead.find_all("tr", recursive=False)), 1)
    count = 0
    for tr in _table_rows(table):
        cells = tr.find_all(["td", "th"], recursive=False)
        if cells and all(cell.name == "th" for cell in cells):
            count += 1
        else:
            break
    return count or 1


def _lossless_int(value: str) -> bool:
    # a numeric type is assigned ONLY if the value round-trips back to its exact source text. rejects "007", "+5",
    # " 5 " etc. so codes/ids with leading zeros stay verbatim strings and are never silently renumbered. also bounded
    # to signed 64-bit so a long numeric id can't overflow the integer cast at query time — it stays a string instead
    if not _INT.fullmatch(value):
        return False
    number = int(value)
    return str(number) == value and -(2**63) <= number <= 2**63 - 1


def _lossless_decimal(value: str) -> bool:
    # Decimal preserves trailing zeros and exact digits ("1.50" stays "1.50"); leading-zero/exponent forms don't
    # round-trip and fall through to string
    return bool(_FLOAT.fullmatch(value)) and str(Decimal(value)) == value


def _dtype(values: list[str]) -> ColumnDType:
    present = [value for value in values if value]
    if not present:
        return ColumnDType.STRING
    if all(_lossless_int(value) for value in present):
        return ColumnDType.INTEGER
    if all(_lossless_int(value) or _lossless_decimal(value) for value in present):
        return ColumnDType.DECIMAL
    return ColumnDType.STRING


def _cast(value: str, dtype: ColumnDType) -> CellValue:
    # never converts: every cell is stored as its exact source text (empty → None). the dtype travels as a hint on the
    # column and the query casts on demand — so dirty cells, codes and ids are never silently altered or dropped
    return None if value == "" else value


def _grid_table(grid: list[list[str]], n_header: int, caption: str | None = None) -> MaterializedTable:
    # deterministic single-table materialization from a verbatim grid: header labels from the header band, every body
    # cell copied verbatim (dtype is only a hint). used as the fallback when the model returns no structure
    n_header = min(max(n_header, 0), len(grid))
    width = len(grid[0]) if grid else 0
    headers = [
        " ".join(dict.fromkeys(grid[row][col] for row in range(n_header) if grid[row][col])) for col in range(width)
    ]
    body = grid[n_header:]
    columns: list[Column] = []
    rows: list[list[CellValue]] = [[None] * width for _ in body]
    for col in range(width):
        values = [row[col] for row in body]
        dtype = _dtype(values)
        columns.append(Column(header=headers[col] or f"col{col}", dtype=dtype))
        for index, value in enumerate(values):
            rows[index][col] = _cast(value, dtype)
    return MaterializedTable(
        sheet_no=0,
        columns=columns,
        rows=rows,
        sample_rows=rows[:SAMPLE_TABLE_ROWS],
        n_rows=len(rows),
        title=None,
        caption=caption,
        notes=[],
        description="",
        anchors=None,
    )


def extract_html_table(html: str) -> MaterializedTable:
    table = BeautifulSoup(html, "lxml").find("table")
    grid = _grid(table) if isinstance(table, Tag) else []
    if not grid:
        return MaterializedTable(0, [], [], [], 0, None, None, [], "", None)
    caption_tag = table.find("caption") if isinstance(table, Tag) else None
    caption = caption_tag.get_text(separator=" ", strip=True) if caption_tag is not None else None
    return _grid_table(grid, _header_count(table), caption)


def _grid_cell(grid: list[list[str]], row: int, col: int) -> str:
    return grid[row][col] if 0 <= row < len(grid) and 0 <= col < len(grid[row]) else ""


def _grid_header(grid: list[list[str]], header_rows: list[int], col: int) -> str | None:
    parts = dict.fromkeys(cell for row in header_rows if (cell := _grid_cell(grid, row, col)))
    return " ".join(parts) or None


def apply_grid_structure(grid: list[list[str]], structure: TableStructure) -> MaterializedTable:
    count = structure.col_end - structure.col_start + 1
    header_rows = structure.header_rows or []
    collected: list[list[str]] = []
    for offset in range(structure.data_start, structure.data_end + 1):
        raw = [_grid_cell(grid, offset, structure.col_start + index) for index in range(count)]
        if all(value == "" for value in raw):
            continue
        collected.append(raw)
    dtypes = [_dtype([raw[index] for raw in collected]) for index in range(count)]
    headers = [_grid_header(grid, header_rows, structure.col_start + index) for index in range(count)]
    columns = [Column(header=headers[index] or f"col{index}", dtype=dtypes[index]) for index in range(count)]
    rows = [[_cast(raw[index], dtypes[index]) for index in range(count)] for raw in collected]
    return MaterializedTable(
        sheet_no=0,
        columns=columns,
        rows=rows,
        sample_rows=rows[:SAMPLE_TABLE_ROWS],
        n_rows=len(rows),
        title=structure.title,
        caption=structure.caption,
        notes=structure.notes or [],
        description=structure.description or "",
        anchors=None,
    )


async def structure_html_tables(html: str) -> list[MaterializedTable]:
    table = BeautifulSoup(html, "lxml").find("table")
    grid = _grid(table) if isinstance(table, Tag) else []
    if not grid:
        return []
    mask = await predict_pooled(grid)
    tables = [apply_grid_structure(grid, spec) for spec in structure_from_mask(grid, mask)]
    return tables or [extract_html_table(html)]


async def structure_csv_tables(data: bytes, separator: str) -> list[MaterializedTable]:
    grid = await asyncio.to_thread(read_csv_grid, data, separator)
    if not grid:
        return []
    mask = await predict_pooled(grid)
    tables = [apply_grid_structure(grid, spec) for spec in structure_from_mask(grid, mask)]
    return tables or [_grid_table(grid, 1)]


def _next(counters: dict[str, int], entity: str) -> int:
    counters[entity] = counters.get(entity, 0) + 1
    return counters[entity]


def _scalar(value: object) -> CellValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return json.dumps(value, ensure_ascii=False)


def _process_field(
    key: str,
    value: object,
    row: dict,
    entity: str,
    row_id: int,
    entities: dict[str, list[dict]],
    counters: dict[str, int],
) -> None:
    if isinstance(value, dict):
        for subkey, subvalue in value.items():
            _process_field(f"{key}.{subkey}", subvalue, row, entity, row_id, entities, counters)
    elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        child = f"{entity}.{key}"
        for item in value:
            child_id = _next(counters, child)
            child_row: dict = {"_id": child_id, f"{entity}_id": row_id}
            for field_key, field_value in item.items():
                _process_field(field_key, field_value, child_row, child, child_id, entities, counters)
            entities.setdefault(child, []).append(child_row)
    else:
        row[key] = _scalar(value)


def normalize_json(data: object, root: str) -> dict[str, list[dict]]:
    entities: dict[str, list[dict]] = {}
    counters: dict[str, int] = {}
    records = data if isinstance(data, list) else [data]
    for record in records:
        fields = record if isinstance(record, dict) else {"value": record}
        row_id = _next(counters, root)
        row: dict = {"_id": row_id}
        for key, value in fields.items():
            _process_field(key, value, row, root, row_id, entities, counters)
        entities.setdefault(root, []).append(row)
    return entities


def _infer_dtype(values: list[CellValue]) -> ColumnDType:
    present = [value for value in values if value is not None]
    if not present:
        return ColumnDType.STRING
    if all(isinstance(value, bool) for value in present):
        return ColumnDType.BOOLEAN
    if all(isinstance(value, int) and not isinstance(value, bool) for value in present):
        return ColumnDType.INTEGER
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in present):
        return ColumnDType.FLOAT
    return ColumnDType.STRING


def _entity_to_table(name: str, rows: list[dict]) -> MaterializedTable:
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    columns = [Column(header=key, dtype=_infer_dtype([row.get(key) for row in rows])) for key in keys]
    data_rows = [[row.get(key) for key in keys] for row in rows]
    return MaterializedTable(
        sheet_no=0,
        columns=columns,
        rows=data_rows,
        sample_rows=data_rows[:SAMPLE_TABLE_ROWS],
        n_rows=len(data_rows),
        title=name,
        caption=None,
        notes=[],
        description="",
        anchors=None,
    )


def extract_json_tables(data: bytes, root: str) -> list[tuple[int, MaterializedTable]]:
    entities = normalize_json(json.loads(data), root)
    return [(ordinal, _entity_to_table(name, rows)) for ordinal, (name, rows) in enumerate(entities.items(), start=1)]


_PARATEXT = {"header", "footer", "page_number", "page_footnote"}


def _columns(html: str) -> int:
    table = BeautifulSoup(html, "lxml").find("table")
    if not isinstance(table, Tag):
        return 0
    grid = _grid(table)
    return len(grid[0]) if grid else 0


def _merge_html(htmls: list[str]) -> str:
    base = BeautifulSoup(htmls[0], "lxml").find("table")
    if not isinstance(base, Tag):
        return htmls[0]
    first_row = base.find("tr")
    header_text = first_row.get_text(strip=True) if first_row is not None else ""
    for html in htmls[1:]:
        other = BeautifulSoup(html, "lxml").find("table")
        if not isinstance(other, Tag):
            continue
        for tr in other.find_all("tr"):
            if tr.get_text(strip=True) != header_text:
                base.append(tr)
    return str(base)


def stitch_tables(blocks: list[Block]) -> list[Block]:
    result: list[Block] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.type != "table":
            result.append(block)
            index += 1
            continue
        group = [block]
        columns = _columns(block.text or "")
        last_page = block.page_idx
        cursor = index + 1
        while cursor < len(blocks):
            following = blocks[cursor]
            if following.type in _PARATEXT or not (following.text or "").strip():
                cursor += 1
                continue
            if (
                following.type == "table"
                and following.page_idx > last_page
                and _columns(following.text or "") == columns
            ):
                group.append(following)
                last_page = following.page_idx
                cursor += 1
                continue
            break
        if len(group) > 1:
            text = _merge_html([item.text or "" for item in group])
            result.append(Block(type="table", page_idx=block.page_idx, text=text))
            index = cursor
        else:
            result.append(block)
            index += 1
    return result
