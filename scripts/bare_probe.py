import asyncio
import logging
import pathlib
import sys

from citadel.llm import call_text
from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.tabular.structure import _render_excel

logger = logging.getLogger("bare")

MAX_TOKENS = 1536
DEFAULT = ("sheet1_region3", "sheet1_region4", "sheet2_region1", "sheet2_region2")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    targets = tuple(sys.argv[1:]) or DEFAULT
    blob = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()

    for sheet in load_all_sheets(blob):
        for index, region in enumerate(find_regions(sheet)):
            label = f"sheet{sheet.sheet_no}_region{index}"
            if label not in targets:
                continue
            grid = region_grid(sheet, region)
            width = max((len(row) for row in grid), default=0)
            rows = list(range(min(len(grid), 40)))
            text = _render_excel(grid, rows, width)
            answer = await call_text(text, max_tokens=MAX_TOKENS, key=f"bare:{label}")
            logger.info("=== %s (%d rows, showing %d) ===\n%s\n", label, len(grid), len(rows), answer)


if __name__ == "__main__":
    asyncio.run(main())
