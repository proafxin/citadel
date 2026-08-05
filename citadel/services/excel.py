import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

import openpyxl
from openpyxl.cell.cell import Cell as OpenpyxlCell
from openpyxl.utils.cell import range_boundaries
from openpyxl.worksheet.worksheet import Worksheet

from citadel.schemas.table import TableStructure
from citadel.services.grid import classify_grid, grid_text
from citadel.tabular.materialize import MaterializedTable
from citadel.tabular.structure import structure_tables

type RawCellValue = str | int | float | bool | datetime | None

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
class SheetTable:
    min_row: int
    min_col: int
    max_row: int
    max_col: int
    header_row_count: int


@dataclass
class SheetPivot:
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
    tables: list[SheetTable]
    pivots: list[SheetPivot]


@dataclass
class Region:
    min_row: int
    min_col: int
    max_row: int
    max_col: int
    cells: list[Cell]


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


def _capture_tables(worksheet: Worksheet) -> list[SheetTable]:
    tables: list[SheetTable] = []
    for table in worksheet.tables.values():
        min_col, min_row, max_col, max_row = range_boundaries(table.ref)
        tables.append(
            SheetTable(
                min_row=min_row,
                min_col=min_col,
                max_row=max_row,
                max_col=max_col,
                header_row_count=table.headerRowCount or 1,
            )
        )
    return tables


def _capture_pivots(worksheet: Worksheet) -> list[SheetPivot]:
    names = set(worksheet.parent.sheetnames)
    pivots: list[SheetPivot] = []
    for pivot in worksheet._pivots:
        ref = getattr(pivot.location, "ref", None)
        source = getattr(pivot.cache.cacheSource, "worksheetSource", None)
        if ref is None or source is None or source.sheet not in names:
            continue
        min_col, min_row, max_col, max_row = range_boundaries(ref)
        pivots.append(SheetPivot(min_row=min_row, min_col=min_col, max_row=max_row, max_col=max_col))
    return pivots


def extract_sheet(values_sheet: Worksheet, formulas_sheet: Worksheet, sheet_no: int) -> SheetExtraction:
    return SheetExtraction(
        sheet_no=sheet_no,
        sheet_name=formulas_sheet.title,
        max_row=formulas_sheet.max_row or 0,
        max_col=formulas_sheet.max_column or 0,
        cells=_capture_cells(values_sheet, formulas_sheet),
        merges=_capture_merges(formulas_sheet),
        tables=_capture_tables(formulas_sheet),
        pivots=_capture_pivots(formulas_sheet),
    )


def sheet_names(data: bytes) -> list[str]:
    workbook = openpyxl.load_workbook(BytesIO(data), read_only=True)
    try:
        return workbook.sheetnames
    finally:
        workbook.close()


def load_all_sheets(data: bytes) -> list[SheetExtraction]:
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


def region_comments(region: Region) -> list[str]:
    ordered = sorted(region.cells, key=lambda cell: (cell.row, cell.col))
    return [cell.comment for cell in ordered if cell.comment]


def _formula_shape(formula: str) -> str:
    return _A1_REF.sub(r"\1#", formula)


def region_formulas(region: Region) -> list[str]:
    ordered = sorted(region.cells, key=lambda cell: (cell.row, cell.col))
    by_shape: dict[str, str] = {}
    for cell in ordered:
        if cell.formula:
            by_shape.setdefault(_formula_shape(cell.formula), cell.formula)
    return list(by_shape.values())


def _anchor_range(region: Region, structure: TableStructure) -> dict:
    top = region.min_row + (min(structure.header_rows) if structure.header_rows else structure.data_start)
    return {
        "min_row": top,
        "min_col": region.min_col + structure.col_start,
        "max_row": region.min_row + structure.data_end,
        "max_col": region.min_col + structure.col_end,
    }


def _region_bounds(region: Region) -> dict:
    return {
        "min_row": region.min_row,
        "min_col": region.min_col,
        "max_row": region.max_row,
        "max_col": region.max_col,
    }


def _render_cell(value: RawCellValue) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def region_grid(sheet: SheetExtraction, region: Region) -> list[list[str]]:
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


def _pivot_covers(region: Region, pivots: list[SheetPivot]) -> bool:
    return any(
        region.min_row <= pivot.max_row
        and region.max_row >= pivot.min_row
        and region.min_col <= pivot.max_col
        and region.max_col >= pivot.min_col
        for pivot in pivots
    )


async def extract_sheet_content(sheet: SheetExtraction) -> list[tuple[int, SheetItem]]:
    text: list[SheetItem] = []
    grids: list[list[list[str]]] = []
    cells: list[Cell] = []
    for region in find_regions(sheet):
        grid = region_grid(sheet, region)
        kind = classify_grid(grid)
        if kind == "empty" or _pivot_covers(region, sheet.pivots):
            continue
        if kind != "table":
            body = " ".join([grid_text(grid), *region_comments(region)]).strip()
            text.append(SheetText(sheet_no=sheet.sheet_no, text=body))
            continue
        grids.append(grid)
        cells.extend(region.cells)
    items: list[SheetItem] = list(text)
    if grids:
        anchors = {
            "min_row": min(cell.row for cell in cells),
            "min_col": min(cell.col for cell in cells),
            "max_row": max(cell.row for cell in cells),
            "max_col": max(cell.col for cell in cells),
        }
        structured = await structure_tables(grids, sheet_no=sheet.sheet_no, anchors=anchors)
        items.extend(table for table, _blocks in structured)
    return list(enumerate(items, start=1))
