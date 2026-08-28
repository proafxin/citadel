import asyncio
import logging
import pathlib

from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.tabular.structure import FULL_RENDER_CELLS

logger = logging.getLogger("dump")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    blob = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()

    for sheet in load_all_sheets(blob):
        for index, region in enumerate(find_regions(sheet)):
            grid = region_grid(sheet, region)
            width = max((len(row) for row in grid), default=0)
            if sum(1 for row in grid for cell in row if cell.strip()) > FULL_RENDER_CELLS:
                continue
            logger.info("=== sheet%d_region%d rows=%d width=%d ===", sheet.sheet_no, index, len(grid), width)
            for row_index, row in enumerate(grid):
                logger.info("  %3d | %s", row_index, " | ".join(cell[:28] for cell in row[:12]))


if __name__ == "__main__":
    asyncio.run(main())
