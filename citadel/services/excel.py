import asyncio
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

import openpyxl
from openpyxl.cell.cell import Cell as OpenpyxlCell
from openpyxl.utils.cell import range_boundaries
from openpyxl.worksheet.worksheet import Worksheet

from citadel.services.grid import classify_grid, grid_text
from citadel.tabular.materialize import MaterializedTable
from citadel.tabular.structure import Position, merge_candidates, structure_candidate

type RawCellValue = str | int | float | bool | datetime | None


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
class SheetTable:
    min_row: int
    min_col: int
    max_row: int
    max_col: int
    header_row_count: int


@dataclass
class SheetExtraction:
    sheet_no: int
    sheet_name: str
    max_row: int
    max_col: int
    cells: list[Cell]
    tables: list[SheetTable]
    images: list[bytes]


@dataclass
class Region:
    min_row: int
    min_col: int
    max_row: int
    max_col: int
    cells: list[Cell]


@dataclass
class TableBounds:
    min_row: int
    min_col: int
    max_row: int
    max_col: int


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


def _capture_images(worksheet: Worksheet) -> list[bytes]:
    return [image._data() for image in worksheet._images]


def extract_sheet(values_sheet: Worksheet, formulas_sheet: Worksheet, sheet_no: int) -> SheetExtraction:
    return SheetExtraction(
        sheet_no=sheet_no,
        sheet_name=formulas_sheet.title,
        max_row=formulas_sheet.max_row or 0,
        max_col=formulas_sheet.max_column or 0,
        cells=_capture_cells(values_sheet, formulas_sheet),
        tables=_capture_tables(formulas_sheet),
        images=_capture_images(formulas_sheet),
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
            for local_row_start, local_row_end in _runs(sorted({cell.row for cell in cells})):
                local_cells = [cell for cell in cells if local_row_start <= cell.row <= local_row_end]
                regions.append(
                    Region(
                        min_row=local_row_start,
                        min_col=col_start,
                        max_row=local_row_end,
                        max_col=col_end,
                        cells=local_cells,
                    )
                )
    return regions


def region_comments(region: Region) -> list[str]:
    ordered = sorted(region.cells, key=lambda cell: (cell.row, cell.col))
    return [cell.comment for cell in ordered if cell.comment]


def _render_cell(value: RawCellValue) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def region_grid(sheet: SheetExtraction, region: Region) -> list[list[str]]:
    values = {(cell.row, cell.col): cell_value(cell) for cell in region.cells}
    return [
        [_render_cell(values.get((row, col))) for col in range(region.min_col, region.max_col + 1)]
        for row in range(region.min_row, region.max_row + 1)
    ]


def _region_anchors(region: Region) -> dict:
    return {"min_row": region.min_row, "min_col": region.min_col, "max_row": region.max_row, "max_col": region.max_col}


async def extract_sheet_content(doc_id: str, sheet: SheetExtraction) -> list[tuple[int, SheetItem]]:
    text: list[SheetItem] = []
    candidates: list[tuple[Region, list[list[str]]]] = []
    for region in find_regions(sheet):
        grid = region_grid(sheet, region)
        kind = classify_grid(grid)
        if kind == "empty":
            continue
        if kind != "table":
            body = " ".join([grid_text(grid), *region_comments(region)]).strip()
            text.append(SheetText(sheet_no=sheet.sheet_no, text=body))
            continue
        candidates.append((region, grid))
    items: list[SheetItem] = list(text)
    if candidates:
        resolved = await asyncio.gather(
            *(
                structure_candidate(
                    grid,
                    key=f"structure:{doc_id}:sheet{sheet.sheet_no}:{index}",
                    known_table=False,
                    sheet_no=sheet.sheet_no,
                    anchors=_region_anchors(region),
                )
                for index, (region, grid) in enumerate(candidates)
            )
        )
        regions: list[Position] = []
        members: list[MaterializedTable] = []
        for (region, _grid), tables in zip(candidates, resolved, strict=True):
            for table in tables:
                anchors = table.anchors
                regions.append(
                    TableBounds(
                        min_row=anchors.get("min_row", region.min_row),
                        min_col=anchors.get("min_col", region.min_col),
                        max_row=anchors.get("max_row", region.max_row),
                        max_col=anchors.get("max_col", region.max_col),
                    )
                )
            members.extend(tables)
        items.extend(await merge_candidates(doc_id, sheet.sheet_no, regions, members))
    return list(enumerate(items, start=1))
