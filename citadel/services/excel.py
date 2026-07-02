from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

import openpyxl
from openpyxl.cell.cell import Cell as OpenpyxlCell
from openpyxl.utils import coordinate_to_tuple, range_boundaries
from openpyxl.worksheet.worksheet import Worksheet

from citadel.llm import call_slm
from citadel.prompts import load_prompt
from citadel.schemas.table import CellValue, Column, ColumnDType, RegionStructure, TableStructure

type RawCellValue = str | int | float | bool | datetime | None

HEAD_ROWS = 4
SAMPLE_CAP = 50
SAMPLE_TABLE_ROWS = 10
CONTEXT_ROWS = 2


@dataclass
class Cell:
    row: int
    col: int
    value: RawCellValue
    number_format: str
    bold: bool
    filled: bool
    bordered: bool


@dataclass
class MergedRange:
    min_row: int
    min_col: int
    max_row: int
    max_col: int


@dataclass
class ExcelTableObject:
    name: str
    min_row: int
    min_col: int
    max_row: int
    max_col: int
    header_rows: int


@dataclass
class SheetExtraction:
    sheet_no: int
    sheet_name: str
    max_row: int
    max_col: int
    cells: list[Cell]
    merges: list[MergedRange]
    table_objects: list[ExcelTableObject]
    comments: dict[tuple[int, int], str]
    freeze_row: int | None
    freeze_col: int | None


@dataclass
class Region:
    min_row: int
    min_col: int
    max_row: int
    max_col: int
    cells: list[Cell]


@dataclass
class RegionAnchors:
    region: Region
    n_rows: int
    n_cols: int
    head: list[list[RawCellValue]]
    samples: list[tuple[int, list[RawCellValue]]]
    bold_offsets: list[int]
    merges: list[MergedRange]
    table_objects: list[ExcelTableObject]
    above_text: list[str]
    below_text: list[str]
    freeze_header_rows: int | None
    comments: list[str]


@dataclass
class MaterializedTable:
    sheet_no: int
    columns: list[Column]
    rows: list[list[CellValue]]
    sample_rows: list[list[CellValue]]
    n_rows: int
    title: str | None
    caption: str | None
    notes: list[str]
    description: str
    anchors: dict


def _style_flags(cell: OpenpyxlCell) -> tuple[bool, bool, bool]:
    bold = bool(cell.font and cell.font.bold)
    fill = cell.fill
    filled = bool(fill and fill.patternType and fill.patternType != "none")
    border = cell.border
    bordered = bool(
        border and any(side and side.style for side in (border.left, border.right, border.top, border.bottom))
    )
    return bold, filled, bordered


def _capture_cells(worksheet: Worksheet) -> tuple[list[Cell], dict[tuple[int, int], str]]:
    cells: list[Cell] = []
    comments: dict[tuple[int, int], str] = {}
    for row in worksheet.iter_rows():
        for cell in row:
            if cell.comment is not None:
                comments[cell.row, cell.column] = cell.comment.text or ""
            if cell.value is None:
                continue
            bold, filled, bordered = _style_flags(cell)
            cells.append(
                Cell(
                    row=cell.row,
                    col=cell.column,
                    value=cell.value,
                    number_format=cell.number_format or "General",
                    bold=bold,
                    filled=filled,
                    bordered=bordered,
                )
            )
    return cells, comments


def _capture_merges(worksheet: Worksheet) -> list[MergedRange]:
    return [
        MergedRange(min_row=rng.min_row, min_col=rng.min_col, max_row=rng.max_row, max_col=rng.max_col)
        for rng in worksheet.merged_cells.ranges
    ]


def _capture_tables(worksheet: Worksheet) -> list[ExcelTableObject]:
    objects: list[ExcelTableObject] = []
    for table in worksheet.tables.values():
        min_col, min_row, max_col, max_row = range_boundaries(table.ref)
        objects.append(
            ExcelTableObject(
                name=table.name,
                min_row=min_row,
                min_col=min_col,
                max_row=max_row,
                max_col=max_col,
                header_rows=table.headerRowCount or 0,
            )
        )
    return objects


def _freeze(worksheet: Worksheet) -> tuple[int | None, int | None]:
    panes = worksheet.freeze_panes
    if not panes:
        return None, None
    row, col = coordinate_to_tuple(panes)
    return row, col


def extract_sheet(worksheet: Worksheet, sheet_no: int) -> SheetExtraction:
    cells, comments = _capture_cells(worksheet)
    freeze_row, freeze_col = _freeze(worksheet)
    return SheetExtraction(
        sheet_no=sheet_no,
        sheet_name=worksheet.title,
        max_row=worksheet.max_row or 0,
        max_col=worksheet.max_column or 0,
        cells=cells,
        merges=_capture_merges(worksheet),
        table_objects=_capture_tables(worksheet),
        comments=comments,
        freeze_row=freeze_row,
        freeze_col=freeze_col,
    )


def sheet_names(data: bytes) -> list[str]:
    return openpyxl.load_workbook(BytesIO(data), read_only=True).sheetnames


def extract_sheet_no(data: bytes, sheet_no: int) -> SheetExtraction:
    workbook = openpyxl.load_workbook(BytesIO(data), data_only=True)
    return extract_sheet(workbook.worksheets[sheet_no - 1], sheet_no)


def _runs(indices: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start = prev = indices[0]
    for index in indices[1:]:
        if index == prev + 1:
            prev = index
        else:
            runs.append((start, prev))
            start = prev = index
    runs.append((start, prev))
    return runs


def find_regions(sheet: SheetExtraction) -> list[Region]:
    if not sheet.cells:
        return []
    by_row: dict[int, list[Cell]] = {}
    for cell in sheet.cells:
        by_row.setdefault(cell.row, []).append(cell)
    regions: list[Region] = []
    for row_start, row_end in _runs(sorted(by_row)):
        band = [cell for row in range(row_start, row_end + 1) for cell in by_row.get(row, [])]
        for col_start, col_end in _runs(sorted({cell.col for cell in band})):
            cells = [cell for cell in band if col_start <= cell.col <= col_end]
            regions.append(
                Region(
                    min_row=min(cell.row for cell in cells),
                    min_col=col_start,
                    max_row=max(cell.row for cell in cells),
                    max_col=col_end,
                    cells=cells,
                )
            )
    return regions


def _value_map(cells: list[Cell]) -> dict[tuple[int, int], RawCellValue]:
    return {(cell.row, cell.col): cell.value for cell in cells}


def _row_values(
    values: dict[tuple[int, int], RawCellValue], row: int, min_col: int, max_col: int
) -> list[RawCellValue]:
    return [values.get((row, col)) for col in range(min_col, max_col + 1)]


def _infer_dtype(values: list[RawCellValue]) -> ColumnDType:
    present = [value for value in values if value is not None]
    if not present:
        return ColumnDType.STRING
    if all(isinstance(value, bool) for value in present):
        return ColumnDType.BOOLEAN
    if all(isinstance(value, int) and not isinstance(value, bool) for value in present):
        return ColumnDType.INTEGER
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in present):
        return ColumnDType.FLOAT
    if all(isinstance(value, datetime) for value in present):
        return ColumnDType.DATETIME
    return ColumnDType.STRING


def _sample_rows(region: Region, head_end: int) -> list[int]:
    body = list(range(head_end + 1, region.max_row + 1))
    if not body:
        return []
    stride = max(1, (len(body) + SAMPLE_CAP - 1) // SAMPLE_CAP)
    picked = set(body[::stride])
    picked.update({body[0], body[-1]})
    return sorted(picked)


def _overlaps(min_row: int, min_col: int, max_row: int, max_col: int, region: Region) -> bool:
    return not (
        max_row < region.min_row or min_row > region.max_row or max_col < region.min_col or min_col > region.max_col
    )


def _text_band(cells: list[Cell], rows: range, min_col: int, max_col: int) -> list[str]:
    return [
        cell.value
        for cell in cells
        if cell.row in rows and min_col <= cell.col <= max_col and isinstance(cell.value, str)
    ]


def build_anchors(sheet: SheetExtraction, region: Region) -> RegionAnchors:
    values = _value_map(region.cells)
    head_end = min(region.min_row + HEAD_ROWS - 1, region.max_row)
    head = [_row_values(values, row, region.min_col, region.max_col) for row in range(region.min_row, head_end + 1)]
    samples = [
        (row - region.min_row, _row_values(values, row, region.min_col, region.max_col))
        for row in _sample_rows(region, head_end)
    ]
    freeze = sheet.freeze_row
    return RegionAnchors(
        region=region,
        n_rows=region.max_row - region.min_row + 1,
        n_cols=region.max_col - region.min_col + 1,
        head=head,
        samples=samples,
        bold_offsets=sorted({cell.row - region.min_row for cell in region.cells if cell.bold}),
        merges=[
            merge
            for merge in sheet.merges
            if _overlaps(merge.min_row, merge.min_col, merge.max_row, merge.max_col, region)
        ],
        table_objects=[
            table
            for table in sheet.table_objects
            if _overlaps(table.min_row, table.min_col, table.max_row, table.max_col, region)
        ],
        above_text=_text_band(
            sheet.cells, range(max(1, region.min_row - CONTEXT_ROWS), region.min_row), region.min_col, region.max_col
        ),
        below_text=_text_band(
            sheet.cells, range(region.max_row + 1, region.max_row + 1 + CONTEXT_ROWS), region.min_col, region.max_col
        ),
        freeze_header_rows=(
            freeze - region.min_row if freeze and region.min_row < freeze <= region.max_row + 1 else None
        ),
        comments=[
            text
            for (row, col), text in sheet.comments.items()
            if region.min_row <= row <= region.max_row and region.min_col <= col <= region.max_col
        ],
    )


def _fmt_row(values: list[RawCellValue]) -> str:
    return " | ".join("" if value is None else str(value) for value in values)


def _rel(region: Region, row: int, col: int) -> str:
    return f"r{row - region.min_row}c{col - region.min_col}"


def _render_anchors(anchors: RegionAnchors) -> str:
    region = anchors.region
    lines = [
        f"Region: {anchors.n_rows} rows x {anchors.n_cols} cols (0-based offsets within the region).",
        "Top rows:",
        *(f"  r{offset}: {_fmt_row(row)}" for offset, row in enumerate(anchors.head)),
    ]
    if anchors.samples:
        lines.append("Sample data rows:")
        lines.extend(f"  r{offset}: {_fmt_row(values)}" for offset, values in anchors.samples)
    if anchors.bold_offsets:
        lines.append(f"Bold row offsets: {anchors.bold_offsets}")
    if anchors.merges:
        merged = ", ".join(
            f"{_rel(region, merge.min_row, merge.min_col)}:{_rel(region, merge.max_row, merge.max_col)}"
            for merge in anchors.merges
        )
        lines.append(f"Merged ranges: {merged}")
    if anchors.table_objects:
        objects = ", ".join(
            f"{table.name}[{_rel(region, table.min_row, table.min_col)}:"
            f"{_rel(region, table.max_row, table.max_col)}] header_rows={table.header_rows}"
            for table in anchors.table_objects
        )
        lines.append(f"Excel table objects: {objects}")
    if anchors.freeze_header_rows is not None:
        lines.append(f"Freeze suggests header rows: {anchors.freeze_header_rows}")
    if anchors.above_text:
        lines.append(f"Text above region: {anchors.above_text}")
    if anchors.below_text:
        lines.append(f"Text below region: {anchors.below_text}")
    if anchors.comments:
        lines.append(f"Comments: {anchors.comments}")
    return "\n".join(lines)


async def extract_structure(anchors: RegionAnchors) -> RegionStructure:
    prompt = f"{load_prompt('table_structure')}\n{_render_anchors(anchors)}"
    return RegionStructure.model_validate(await call_slm(prompt, RegionStructure.model_json_schema()))


def _cast(value: RawCellValue, dtype: ColumnDType) -> CellValue:
    if value is None:
        return None
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    if dtype == ColumnDType.INTEGER and numeric:
        return int(value)
    if dtype in {ColumnDType.FLOAT, ColumnDType.DECIMAL} and numeric:
        return float(value)
    if dtype == ColumnDType.BOOLEAN and isinstance(value, bool):
        return value
    if dtype in {ColumnDType.DATE, ColumnDType.DATETIME} and isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _sample(rows: list[list[CellValue]]) -> list[list[CellValue]]:
    if len(rows) <= SAMPLE_TABLE_ROWS:
        return list(rows)
    step = len(rows) / SAMPLE_TABLE_ROWS
    return [rows[int(index * step)] for index in range(SAMPLE_TABLE_ROWS)]


def _anchor_range(region: Region, structure: TableStructure) -> dict:
    top = region.min_row + (min(structure.header_rows) if structure.header_rows else structure.data_start)
    return {
        "min_row": top,
        "min_col": region.min_col + structure.col_start,
        "max_row": region.min_row + structure.data_end,
        "max_col": region.min_col + structure.col_end,
    }


def _header_at(values: dict[tuple[int, int], RawCellValue], header_rows: list[int], col: int) -> str | None:
    parts: dict[str, None] = {}
    for row in header_rows:
        raw = values.get((row, col))
        if raw is not None and (text := str(raw).strip()):
            parts[text] = None
    return " ".join(parts) or None


def apply_structure(region: Region, structure: TableStructure, sheet_no: int) -> MaterializedTable:
    values = _value_map(region.cells)
    count = structure.col_end - structure.col_start + 1
    header_rows = [region.min_row + offset for offset in (structure.header_rows or [])]
    raw_rows: list[list[RawCellValue]] = []
    for offset in range(structure.data_start, structure.data_end + 1):
        abs_row = region.min_row + offset
        raw = [values.get((abs_row, region.min_col + structure.col_start + index)) for index in range(count)]
        if all(value is None for value in raw):
            continue
        raw_rows.append(raw)
    dtypes = [_infer_dtype([raw[index] for raw in raw_rows]) for index in range(count)]
    headers = [_header_at(values, header_rows, region.min_col + structure.col_start + index) for index in range(count)]
    columns = [Column(header=headers[index] or f"col{index}", dtype=dtypes[index]) for index in range(count)]
    rows = [[_cast(raw[index], dtypes[index]) for index in range(count)] for raw in raw_rows]
    return MaterializedTable(
        sheet_no=sheet_no,
        columns=columns,
        rows=rows,
        sample_rows=_sample(rows),
        n_rows=len(rows),
        title=structure.title,
        caption=structure.caption,
        notes=structure.notes or [],
        description=structure.description or "",
        anchors=_anchor_range(region, structure),
    )


async def extract_tables(sheet: SheetExtraction) -> list[tuple[int, MaterializedTable]]:
    tables: list[tuple[int, MaterializedTable]] = []
    ordinal = 0
    for region in find_regions(sheet):
        structure = await extract_structure(build_anchors(sheet, region))
        for table in structure.tables:
            ordinal += 1
            tables.append((ordinal, apply_structure(region, table, sheet.sheet_no)))
    return tables
