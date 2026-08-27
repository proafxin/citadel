import asyncio
import time

from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.tabular.structure import _detect_boundaries


async def main() -> None:
    with open("/home/masterkenway/Downloads/ocr_input/test(1).xlsx", "rb") as f:
        data = f.read()
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
        boxes = await _detect_boundaries(grid, key=f"step1verify:{label}")
        region_elapsed = time.monotonic() - region_start
        print(f"{label:50s} grid_rows={len(grid):5d} elapsed={region_elapsed:7.2f}s boxes={boxes}", flush=True)

    total_elapsed = time.monotonic() - start
    print(f"\nTOTAL elapsed={total_elapsed:.2f}s across {len(targets)} regions", flush=True)


asyncio.run(main())
