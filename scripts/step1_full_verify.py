import asyncio
import pathlib
import time

from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.tabular.structure import _detect_boundaries


async def main() -> None:
    data = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()
    sheets = load_all_sheets(data)
    targets = []
    for sheet in sheets:
        regions = find_regions(sheet)
        for index, region in enumerate(regions):
            grid = region_grid(sheet, region)
            label = f"sheet{sheet.sheet_no}_region{index}_r{region.min_row}-{region.max_row}_c{region.min_col}-{region.max_col}"
            targets.append((label, grid))

    start = time.monotonic()
    for label, grid in targets:
        region_start = time.monotonic()
        await _detect_boundaries(grid, key=f"step1verify:{label}")
        time.monotonic() - region_start

    time.monotonic() - start


asyncio.run(main())
