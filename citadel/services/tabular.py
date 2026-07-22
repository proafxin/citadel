import asyncio
import json
from io import BytesIO

import polars as pl
from bs4 import BeautifulSoup
from bs4.element import Tag

from citadel.schemas.content import Block
from citadel.schemas.table import CellValue, TableStructure
from citadel.tabular.materialize import MaterializedTable, materialize


def read_csv_grid(data: bytes, separator: str) -> list[list[str]]:
    # polars does the parsing — quoting, embedded newlines, ragged lines — and with has_header=True it also SKIPS any
    # leading blank lines and names the columns from the first non-empty row. has_header=False cannot: it takes the
    # column count from the literal first line, so one leading blank line collapses the file to a single column and
    # truncate_ragged_lines then discards the rest of every row. the names come back as the grid's row 0, so the header
    # is data like any other cell. every column stays Utf8 (infer_schema_length=0) so values are exact — dtypes are
    # derived downstream by lossless round-trip, never by polars inference, and "007" stays the string "007"
    frame = pl.read_csv(
        BytesIO(data), separator=separator, has_header=True, infer_schema_length=0, truncate_ragged_lines=True
    )
    rows = [["" if value is None else str(value) for value in row] for row in frame.iter_rows()]
    return [list(frame.columns), *rows]


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


GRID_MAX_SPAN = 1000  # a single cell's row/col span is clamped here; real headers span a handful, only bombs span more
GRID_MAX_CELLS = 5_000_000  # hard ceiling on a materialized grid so a hallucinated/oversized span can't OOM the merge


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
            colspan = min(max(int(cell.get("colspan") or 1), 1), GRID_MAX_SPAN)
            rowspan = min(max(int(cell.get("rowspan") or 1), 1), GRID_MAX_SPAN)
            for delta_row in range(rowspan):
                for delta_col in range(colspan):
                    occupied[row_idx + delta_row, col + delta_col] = value
            if len(occupied) > GRID_MAX_CELLS:
                raise ValueError("table grid exceeds cell cap")
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


def html_to_text(html: str) -> str:
    return BeautifulSoup(html, "lxml").get_text(separator=" ", strip=True)


def grid_from_html(html: str) -> list[list[str]]:
    table = BeautifulSoup(html, "lxml").find("table")
    return _grid(table) if isinstance(table, Tag) else []


def single_table_structure(grid: list[list[str]], header_rows: int) -> TableStructure:
    # the whole grid IS the table: one schema, its header on top. no model decides this — it is the definition of the
    # format. only a raw spreadsheet cell grid, where tables can start anywhere and there may be several, is ambiguous
    # enough to need the model
    return TableStructure(
        col_start=0,
        col_end=max(len(row) for row in grid) - 1,
        header_rows=list(range(header_rows)),
        data_start=header_rows,
        data_end=len(grid) - 1,
    )


def structure_html_tables(html: str) -> list[MaterializedTable]:
    # a <table> is ONE table with its header already marked — by <thead>/<th> when the source is docx/html/pptx, and by
    # paddle's own <ched> verdict when it came out of a pdf. nothing here is inferred, so no model is asked
    table = BeautifulSoup(html, "lxml").find("table")
    if not isinstance(table, Tag):
        return []
    grid = _grid(table)
    if not grid:
        return []
    structured = materialize(grid, single_table_structure(grid, _header_count(table)))
    # a structure that yields no data rows is not a table — drop it rather than storing an empty relation
    return [structured] if structured.n_rows else []


async def structure_csv_tables(data: bytes, separator: str) -> list[MaterializedTable]:
    # a csv/tsv carries ONE schema by construction: polars parses it (quoting, embedded newlines, ragged lines) and the
    # first row names the columns. no structure call, and no model at all
    grid = await asyncio.to_thread(read_csv_grid, data, separator)
    if not grid:
        return []
    return [materialize(grid, single_table_structure(grid, 1))]


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


def _entity_grid(rows: list[dict]) -> list[list[str]]:
    keys = list(dict.fromkeys(key for row in rows for key in row))
    body = [["" if (value := row.get(key)) is None else str(value) for key in keys] for row in rows]
    return [keys, *body]


def extract_json_tables(data: bytes, root: str) -> list[tuple[int, MaterializedTable]]:
    # normalized into relations first (nested objects flattened, nested lists become child entities with a foreign key),
    # so every entity is one table whose keys ARE its schema — deterministic, no structure call
    entities = normalize_json(json.loads(data), root)
    out: list[tuple[int, MaterializedTable]] = []
    for name, rows in entities.items():
        grid = _entity_grid(rows)
        table = materialize(grid, single_table_structure(grid, 1), extra_notes=[name])
        if table.n_rows:
            out.append((len(out) + 1, table))
    return out


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
