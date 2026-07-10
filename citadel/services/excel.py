import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

import openpyxl
from openpyxl.cell.cell import Cell as OpenpyxlCell
from openpyxl.worksheet.worksheet import Worksheet

from citadel.schemas.table import CellValue, Column, ColumnDType, TableStructure
from citadel.services.grid import classify_grid, grid_text
from citadel.tabular.infer import merge_spurious_splits, predict_pooled, structure_from_mask

type RawCellValue = str | int | float | bool | datetime | None

SAMPLE_TABLE_ROWS = 10
_A1_REF = re.compile(r"(\$?[A-Za-z]{1,3})\$?\d+")


@dataclass
class Cell:
    row: int
    col: int
    value: RawCellValue
    number_format: str
    bold: bool
    filled: bool
    bordered: bool
    formula: str | None = None
    comment: str | None = None


def cell_value(cell: Cell) -> RawCellValue:
    return cell.value if cell.value is not None else cell.formula


@dataclass
class MergedRange:
    min_row: int
    min_col: int
    max_row: int
    max_col: int


@dataclass
class SheetExtraction:
    sheet_no: int
    sheet_name: str
    max_row: int
    max_col: int
    cells: list[Cell]
    merges: list[MergedRange]


@dataclass
class Region:
    min_row: int
    min_col: int
    max_row: int
    max_col: int
    cells: list[Cell]


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
    formulas: list[str] | None = None


@dataclass
class SheetText:
    sheet_no: int
    text: str


type SheetItem = MaterializedTable | SheetText


def _style_flags(cell: OpenpyxlCell) -> tuple[bool, bool, bool]:
    bold = bool(cell.font and cell.font.bold)
    fill = cell.fill
    filled = bool(fill and fill.patternType and fill.patternType != "none")
    border = cell.border
    bordered = bool(
        border and any(side and side.style for side in (border.left, border.right, border.top, border.bottom))
    )
    return bold, filled, bordered


def _cell_comment(cell: OpenpyxlCell) -> str | None:
    if cell.comment is None:
        return None
    return cell.comment.text.strip() or None


def _cell_formula(cell: OpenpyxlCell) -> str | None:
    value = cell.value
    return value if isinstance(value, str) and value.startswith("=") else None


def _capture_cells(values_sheet: Worksheet, formulas_sheet: Worksheet) -> list[Cell]:
    cells: list[Cell] = []
    for row in formulas_sheet.iter_rows():
        for cell in row:
            formula = _cell_formula(cell)
            comment = _cell_comment(cell)
            value = values_sheet.cell(row=cell.row, column=cell.column).value
            if value is None and formula is None and comment is None:
                continue
            bold, filled, bordered = _style_flags(cell)
            cells.append(
                Cell(
                    row=cell.row,
                    col=cell.column,
                    value=value,
                    number_format=cell.number_format or "General",
                    bold=bold,
                    filled=filled,
                    bordered=bordered,
                    formula=formula,
                    comment=comment,
                )
            )
    return cells


def _capture_merges(worksheet: Worksheet) -> list[MergedRange]:
    return [
        MergedRange(min_row=rng.min_row, min_col=rng.min_col, max_row=rng.max_row, max_col=rng.max_col)
        for rng in worksheet.merged_cells.ranges
    ]


def extract_sheet(values_sheet: Worksheet, formulas_sheet: Worksheet, sheet_no: int) -> SheetExtraction:
    return SheetExtraction(
        sheet_no=sheet_no,
        sheet_name=formulas_sheet.title,
        max_row=formulas_sheet.max_row or 0,
        max_col=formulas_sheet.max_column or 0,
        cells=_capture_cells(values_sheet, formulas_sheet),
        merges=_capture_merges(formulas_sheet),
    )


def sheet_names(data: bytes) -> list[str]:
    workbook = openpyxl.load_workbook(BytesIO(data), read_only=True)
    try:
        return workbook.sheetnames
    finally:
        workbook.close()  # read_only mode holds the archive open until closed


def load_all_sheets(data: bytes) -> list[SheetExtraction]:
    # two loads: data_only=True yields cached values but no formulas, data_only=False yields formulas but no values.
    # openpyxl offers no mode that gives both, and a generated workbook has NO cached values at all — reading only the
    # values load silently skips every formula cell. callers cache the result per document and index by sheet_no.
    values_workbook = openpyxl.load_workbook(BytesIO(data), data_only=True)
    formulas_workbook = openpyxl.load_workbook(BytesIO(data), data_only=False)
    try:
        return [
            extract_sheet(values_workbook.worksheets[index], formulas_sheet, index + 1)
            for index, formulas_sheet in enumerate(formulas_workbook.worksheets)
        ]
    finally:
        values_workbook.close()
        formulas_workbook.close()


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
    return {(cell.row, cell.col): cell_value(cell) for cell in cells}


def region_comments(region: Region) -> list[str]:
    ordered = sorted(region.cells, key=lambda cell: (cell.row, cell.col))
    return [cell.comment for cell in ordered if cell.comment]


def _formula_shape(formula: str) -> str:
    return _A1_REF.sub(r"\1#", formula)


def region_formulas(region: Region) -> list[str]:
    # a column is ONE formula pattern repeated down its rows with shifted refs, so "=B2*C2" and "=B3*C3" are the same
    # fact. dedupe on the row-stripped shape and keep the first real formula per shape: N rows collapse to one entry
    ordered = sorted(region.cells, key=lambda cell: (cell.row, cell.col))
    by_shape: dict[str, str] = {}
    for cell in ordered:
        if cell.formula:
            by_shape.setdefault(_formula_shape(cell.formula), cell.formula)
    return list(by_shape.values())


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
        notes=[*(structure.notes or []), *region_comments(region)],
        description=structure.description or "",
        anchors=_anchor_range(region, structure),
        formulas=region_formulas(region) or None,
    )


def _render_cell(value: RawCellValue) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def region_grid(sheet: SheetExtraction, region: Region) -> list[list[str]]:
    # the region as a dense string grid for the header model: merged cells are filled (top-left value spans the whole
    # merge) and every typed value rendered to text, so a merged / multi-row header reads like a normal grid. offsets
    # are region-relative (row 0 = region.min_row) to line up with structure_from_mask and apply_structure
    values = {(cell.row, cell.col): cell_value(cell) for cell in region.cells}
    for merge in sheet.merges:
        if merge.max_row < region.min_row or merge.min_row > region.max_row:
            continue
        if merge.max_col < region.min_col or merge.min_col > region.max_col:
            continue
        top_left = values.get((merge.min_row, merge.min_col))
        if top_left is None:
            continue
        for row in range(max(merge.min_row, region.min_row), min(merge.max_row, region.max_row) + 1):
            for col in range(max(merge.min_col, region.min_col), min(merge.max_col, region.max_col) + 1):
                values.setdefault((row, col), top_left)
    return [
        [_render_cell(values.get((row, col))) for col in range(region.min_col, region.max_col + 1)]
        for row in range(region.min_row, region.max_row + 1)
    ]


async def extract_sheet_content(sheet: SheetExtraction) -> list[tuple[int, SheetItem]]:
    items: list[tuple[int, SheetItem]] = []
    ordinal = 0
    for region in find_regions(sheet):
        grid = region_grid(sheet, region)
        kind = classify_grid(grid)
        if kind == "empty":
            continue
        if kind != "table":
            ordinal += 1
            text = " ".join([grid_text(grid), *region_comments(region)]).strip()
            items.append((ordinal, SheetText(sheet_no=sheet.sheet_no, text=text)))
            continue
        mask = await predict_pooled(grid)
        for structure in merge_spurious_splits(grid, structure_from_mask(grid, mask)):
            ordinal += 1
            items.append((ordinal, apply_structure(region, structure, sheet.sheet_no)))
    return items
