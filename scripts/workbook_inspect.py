import logging
import pathlib
import sys
from io import BytesIO

import openpyxl

from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.services.grid import classify_grid
from citadel.tabular.structure import FULL_RENDER_CELLS

logger = logging.getLogger("workbook")

PREVIEW_ROWS = 6
PREVIEW_COLS = 10
CELL_WIDTH = 22


def _preview(grid: list[list[str]], indices: list[int]) -> None:
    for index in indices:
        cells = [cell[:CELL_WIDTH] for cell in grid[index][:PREVIEW_COLS]]
        logger.info("      %4d | %s", index, " | ".join(cells))


def _presentation(blob: bytes) -> dict[int, tuple[list[str], int, int]]:
    workbook = openpyxl.load_workbook(BytesIO(blob), data_only=False)
    try:
        found: dict[int, tuple[list[str], int, int]] = {}
        for index, worksheet in enumerate(workbook.worksheets, start=1):
            merges = [str(item) for item in worksheet.merged_cells.ranges]
            wrapped = 0
            indented = 0
            for row in worksheet.iter_rows():
                for cell in row:
                    alignment = cell.alignment
                    if alignment is None:
                        continue
                    wrapped += bool(alignment.wrap_text)
                    indented += bool(alignment.indent)
            found[index] = (merges, wrapped, indented)
        return found
    finally:
        workbook.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    for arg in sys.argv[1:]:
        path = pathlib.Path(arg)
        logger.info("########## %s (%.1f KB) ##########", path.name, path.stat().st_size / 1024)
        blob = path.read_bytes()
        presentation = _presentation(blob)
        sheets = load_all_sheets(blob)
        for sheet in sheets:
            merges, wrapped, indented = presentation.get(sheet.sheet_no, ([], 0, 0))
            logger.info(
                "~~~ sheet%d merged_ranges=%d wrap_text=%d indented=%d  first_merges=%s",
                sheet.sheet_no,
                len(merges),
                wrapped,
                indented,
                merges[:8],
            )
            formulas = [cell for cell in sheet.cells if cell.formula]
            comments = [cell for cell in sheet.cells if cell.comment]
            merged = [cell for cell in sheet.cells if cell.bold]
            logger.info(
                "=== sheet%d %r  %dx%d  cells=%d  formulas=%d  native_tables=%d  comments=%d  bold=%d",
                sheet.sheet_no,
                sheet.sheet_name,
                sheet.max_row,
                sheet.max_col,
                len(sheet.cells),
                len(formulas),
                len(sheet.tables),
                len(comments),
                len(merged),
            )
            for table in sheet.tables:
                logger.info(
                    "    native table rows %d-%d cols %d-%d header_rows=%d",
                    table.min_row,
                    table.max_row,
                    table.min_col,
                    table.max_col,
                    table.header_row_count,
                )
            for formula in formulas[:5]:
                logger.info("    formula r%dc%d: %s", formula.row, formula.col, formula.formula[:80])
            regions = find_regions(sheet)
            logger.info("    regions=%d", len(regions))
            for index, region in enumerate(regions):
                grid = region_grid(sheet, region)
                width = max((len(row) for row in grid), default=0)
                populated = sum(1 for row in grid for cell in row if cell.strip())
                logger.info(
                    "    -- region%d rows=%d width=%d cells=%d kind=%s render=%s",
                    index,
                    len(grid),
                    width,
                    populated,
                    classify_grid(grid),
                    "sampled" if populated > FULL_RENDER_CELLS else "full",
                )
                head = list(range(min(PREVIEW_ROWS, len(grid))))
                _preview(grid, head)
                if len(grid) > PREVIEW_ROWS * 2:
                    logger.info("      ...")
                    _preview(grid, list(range(len(grid) - PREVIEW_ROWS, len(grid))))


if __name__ == "__main__":
    main()
