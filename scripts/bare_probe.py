import asyncio
import logging
import pathlib
import sys

from citadel.llm import call_text
from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.tabular.structure import _row_line

logger = logging.getLogger("bare")

MAX_TOKENS = 3072
SHOW_ROWS = 40


async def _probe(label: str, grid: list[list[str]]) -> tuple[str, int, int, str]:
    width = max((len(row) for row in grid), default=0)
    rows = list(range(min(len(grid), SHOW_ROWS)))
    header = f"{len(grid)} rows, {width} cols"
    text = "\n".join([header, *(_row_line(index, grid[index], width, None) for index in rows)])
    answer = await call_text(text, max_tokens=MAX_TOKENS, key=f"bare:{label}")
    return label, len(grid), len(rows), answer


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    targets = tuple(sys.argv[1:])
    blob = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()

    selected: list[tuple[str, list[list[str]]]] = []
    for sheet in load_all_sheets(blob):
        for index, region in enumerate(find_regions(sheet)):
            label = f"sheet{sheet.sheet_no}_region{index}"
            if targets and label not in targets:
                continue
            selected.append((label, region_grid(sheet, region)))

    results = await asyncio.gather(*(_probe(label, grid) for label, grid in selected))
    for label, total, shown, answer in results:
        logger.info("=== %s (%d rows, showing %d) ===\n%s\n", label, total, shown, answer)


if __name__ == "__main__":
    asyncio.run(main())
