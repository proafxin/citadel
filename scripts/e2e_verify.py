import asyncio
import logging
import pathlib
import time

from citadel.services.excel import find_regions, load_all_sheets, region_grid, region_values
from citadel.tabular.structure import EXCEL_CHUNK_BUDGET, _chunk_rows, structure_candidate

logger = logging.getLogger("e2e")

TARGETS = ("sheet1_region6", "sheet1_region8", "sheet1_region9", "sheet1_region12")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    blob = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()

    for sheet in load_all_sheets(blob):
        for index, region in enumerate(find_regions(sheet)):
            label = f"sheet{sheet.sheet_no}_region{index}"
            if label not in TARGETS:
                continue
            grid = region_grid(sheet, region)
            values = region_values(sheet, region)
            width = max((len(row) for row in grid), default=0)
            chunks = len(_chunk_rows(grid, width, EXCEL_CHUNK_BUDGET))

            start = time.monotonic()
            tables = await structure_candidate(
                grid,
                key=f"e2e:{label}",
                known_table=False,
                sheet_no=sheet.sheet_no,
                anchors={},
                values=values,
            )
            elapsed = time.monotonic() - start

            path = "large" if chunks > 1 else "small"
            logger.info(
                "%s rows=%d cols=%d chunks=%d path=%s tables=%d elapsed=%.1fs",
                label,
                len(grid),
                width,
                chunks,
                path,
                len(tables),
                elapsed,
            )
            for position, table in enumerate(tables):
                logger.info(
                    "    table[%d] n_rows=%d columns=%d header_rows=%s metadata=%s headers=%s",
                    position,
                    table.n_rows,
                    len(table.columns),
                    table.header_rows,
                    table.anchors.get("metadata_rows"),
                    [column.header for column in table.columns[:6]],
                )


if __name__ == "__main__":
    asyncio.run(main())
