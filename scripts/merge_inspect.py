import asyncio
import logging
import pathlib

from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.tabular.structure import _candidate_summary, structure_candidate

logger = logging.getLogger("merge")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    blob = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()

    for sheet in load_all_sheets(blob):
        if sheet.sheet_no != 2:
            continue
        for index, region in enumerate(find_regions(sheet)):
            grid = region_grid(sheet, region)
            logger.info("region%d rows=%d first_rows=%s", index, len(grid), [row[:6] for row in grid[:3]])
            tables = await structure_candidate(
                grid,
                key=f"merge:{index}",
                known_table=False,
                sheet_no=sheet.sheet_no,
                anchors={},
            )
            for position, table in enumerate(tables):
                logger.info("    table[%d] title=%r n_rows=%d", position, table.title, table.n_rows)
                logger.info("    summary -> %s", _candidate_summary(position, table)[:400])


if __name__ == "__main__":
    asyncio.run(main())
