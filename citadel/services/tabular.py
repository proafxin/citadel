import json
import re
from datetime import date, datetime
from io import BytesIO

import polars as pl
from bs4 import BeautifulSoup
from bs4.element import Tag

from citadel.llm import call_slm
from citadel.prompts import load_prompt
from citadel.schemas.content import Block
from citadel.schemas.table import CellValue, Column, ColumnDType, ColumnRole, RegionStructure, TableStructure
from citadel.services.excel import SAMPLE_TABLE_ROWS, MaterializedTable

_DTYPE_BY_PREFIX = {
    "Int": ColumnDType.INTEGER,
    "UInt": ColumnDType.INTEGER,
    "Float": ColumnDType.FLOAT,
    "Decimal": ColumnDType.DECIMAL,
    "Boolean": ColumnDType.BOOLEAN,
    "Datetime": ColumnDType.DATETIME,
    "Date": ColumnDType.DATE,
}
_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+\.\d+")


def _map_dtype(dtype: pl.DataType) -> ColumnDType:
    name = str(dtype)
    for prefix, mapped in _DTYPE_BY_PREFIX.items():
        if name.startswith(prefix):
            return mapped
    return ColumnDType.STRING


def _cell(value: object) -> CellValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def read_csv_table(data: bytes, separator: str) -> MaterializedTable:
    frame = pl.read_csv(BytesIO(data), separator=separator, infer_schema_length=10000, truncate_ragged_lines=True)
    frame = frame.filter(~pl.all_horizontal(pl.all().is_null()))
    columns = [Column(header=name, dtype=_map_dtype(dtype)) for name, dtype in frame.schema.items()]
    rows = [[_cell(value) for value in row] for row in frame.iter_rows()]
    return MaterializedTable(
        sheet_no=0,
        columns=columns,
        rows=rows,
        sample_rows=rows[:SAMPLE_TABLE_ROWS],
        n_rows=len(rows),
        title=None,
        caption=None,
        notes=[],
        description="",
        anchors=None,
    )


def _grid(table: Tag) -> list[list[str]]:
    occupied: dict[tuple[int, int], str] = {}
    width = 0
    height = 0
    for row_idx, tr in enumerate(table.find_all("tr")):
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
    thead = table.find("thead")
    if thead is not None:
        return max(len(thead.find_all("tr")), 1)
    count = 0
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if cells and all(cell.name == "th" for cell in cells):
            count += 1
        else:
            break
    return count or 1


def _dtype(values: list[str]) -> ColumnDType:
    present = [value for value in values if value]
    if not present:
        return ColumnDType.STRING
    if all(_INT.fullmatch(value) for value in present):
        return ColumnDType.INTEGER
    if all(_INT.fullmatch(value) or _FLOAT.fullmatch(value) for value in present):
        return ColumnDType.FLOAT
    return ColumnDType.STRING


def _cast(value: str, dtype: ColumnDType) -> CellValue:
    if value == "":
        return None
    if dtype == ColumnDType.INTEGER:
        return int(value)
    if dtype == ColumnDType.FLOAT:
        return float(value)
    return value


def extract_html_table(html: str) -> MaterializedTable:
    table = BeautifulSoup(html, "lxml").find("table")
    grid = _grid(table) if isinstance(table, Tag) else []
    if not grid:
        return MaterializedTable(0, [], [], [], 0, None, None, [], "", None)
    caption_tag = table.find("caption") if isinstance(table, Tag) else None
    caption = caption_tag.get_text(separator=" ", strip=True) if caption_tag is not None else None
    n_header = min(_header_count(table), len(grid))
    width = len(grid[0])
    headers = [
        " ".join(dict.fromkeys(grid[row][col] for row in range(n_header) if grid[row][col])) for col in range(width)
    ]
    body = grid[n_header:]
    columns: list[Column] = []
    rows: list[list[CellValue]] = [[None] * width for _ in body]
    for col in range(width):
        values = [row[col] for row in body]
        dtype = _dtype(values)
        columns.append(Column(header=headers[col] or None, dtype=dtype))
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


_GRID_HEAD_ROWS = 4
_GRID_SAMPLE_ROWS = 6


def _render_grid(grid: list[list[str]], context: str) -> str:
    height = len(grid)
    width = len(grid[0]) if grid else 0
    head_end = min(_GRID_HEAD_ROWS, height)
    lines = [
        f"Source: {context}",
        f"Region: {height} rows x {width} cols (0-based offsets within the region).",
        "Top rows:",
        *(f"  r{offset}: {' | '.join(grid[offset])}" for offset in range(head_end)),
    ]
    body = list(range(head_end, height))
    if body:
        step = max(len(body) // _GRID_SAMPLE_ROWS, 1)
        lines.append("Sample data rows:")
        lines.extend(f"  r{offset}: {' | '.join(grid[offset])}" for offset in body[::step][:_GRID_SAMPLE_ROWS])
    return "\n".join(lines)


def _grid_cell(grid: list[list[str]], row: int, col: int) -> str:
    return grid[row][col] if 0 <= row < len(grid) and 0 <= col < len(grid[row]) else ""


def apply_grid_structure(grid: list[list[str]], structure: TableStructure) -> MaterializedTable:
    count = len(structure.columns)
    relative = structure.section_label_col - structure.col_start if structure.section_label_col is not None else -1
    section_i = relative if 0 <= relative < count else None
    collected: list[tuple[CellValue, list[str]]] = []
    section: CellValue = None
    for offset in range(structure.data_start, structure.data_end + 1):
        raw = [_grid_cell(grid, offset, structure.col_start + index) for index in range(count)]
        if all(value == "" for value in raw):
            continue
        if section_i is not None and raw[section_i] and all(v == "" for i, v in enumerate(raw) if i != section_i):
            section = raw[section_i]
            continue
        collected.append((section, raw))
    dtypes = [_dtype([raw[index] for _, raw in collected]) for index in range(count)]
    data_columns = [
        Column(header=col.header or None, dtype=dtypes[index], role=col.role, unit=col.unit)
        for index, col in enumerate(structure.columns)
    ]
    columns = (
        [Column(header="section", role=ColumnRole.SECTION), *data_columns] if section_i is not None else data_columns
    )
    rows: list[list[CellValue]] = []
    for sect, raw in collected:
        cast = [_cast(value, dtypes[index]) for index, value in enumerate(raw)]
        rows.append([sect, *cast] if section_i is not None else cast)
    return MaterializedTable(
        sheet_no=0,
        columns=columns,
        rows=rows,
        sample_rows=rows[:SAMPLE_TABLE_ROWS],
        n_rows=len(rows),
        title=structure.title,
        caption=structure.caption,
        notes=structure.notes,
        description=structure.description,
        anchors=None,
    )


async def structure_html_tables(html: str, context: str) -> list[MaterializedTable]:
    table = BeautifulSoup(html, "lxml").find("table")
    grid = _grid(table) if isinstance(table, Tag) else []
    if not grid:
        return []
    prompt = f"{load_prompt('table_structure')}\n{_render_grid(grid, context)}"
    structure = RegionStructure.model_validate(await call_slm(prompt, RegionStructure.model_json_schema()))
    tables = [apply_grid_structure(grid, spec) for spec in structure.tables]
    return tables or [extract_html_table(html)]


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
