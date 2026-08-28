import asyncio
import collections
import logging
import pathlib
import sys

from citadel.services.excel import find_regions, load_all_sheets, region_grid, region_values
from citadel.tabular.structure import _anchor_boundaries

logger = logging.getLogger("region9")
TARGET = "sheet1_region9"
HEAD = 6
TAIL = 3


async def _attempt(grid: list[list[str]], values: list[list[object]], attempt: int) -> list[tuple[int, int, int, int]]:
    boxes, _scanned = await _anchor_boundaries(grid, values, key=f"probe:{TARGET}:{attempt}")
    return boxes


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    blob = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()

    for sheet in load_all_sheets(blob):
        for index, region in enumerate(find_regions(sheet)):
            if f"sheet{sheet.sheet_no}_region{index}" != TARGET:
                continue
            grid = region_grid(sheet, region)
            values = region_values(sheet, region)
            logger.info("%s rows=%d width=%d", TARGET, len(grid), max(len(row) for row in grid))
            for row in range(HEAD):
                logger.info("  head %3d | %s", row, " | ".join(cell[:24] for cell in grid[row][:10]))
            for row in range(len(grid) - TAIL, len(grid)):
                logger.info("  tail %3d | %s", row, " | ".join(cell[:24] for cell in grid[row][:10]))

            results = await asyncio.gather(*(_attempt(grid, values, attempt) for attempt in range(runs)))
            counts: collections.Counter[str] = collections.Counter()
            for attempt, boxes in enumerate(results):
                logger.info("  attempt=%d n=%d %s", attempt, len(boxes), boxes)
                counts[str(boxes)] += 1
            logger.info("  distribution over %d runs: %s", runs, dict(counts))


if __name__ == "__main__":
    asyncio.run(main())
