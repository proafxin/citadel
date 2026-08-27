import asyncio
import logging
import pathlib
from itertools import starmap

from citadel.services.excel import find_regions, load_all_sheets, region_grid, region_values
from citadel.tabular.structure import (
    EXCEL_CHUNK_BUDGET,
    _anchor_boundaries,
    _chunk_rows,
    _detect_boundaries,
    _scan_anomalies,
    _slice_grid,
    _type_rows,
    _typing_rows,
)

logger = logging.getLogger("titles")


async def _probe(label: str, grid: list[list[str]], values: list[list[object]]) -> None:
    width = max((len(row) for row in grid), default=0)
    large = len(_chunk_rows(grid, width, EXCEL_CHUNK_BUDGET)) > 1
    if large:
        boxes, _scanned = await _anchor_boundaries(grid, values, key=f"title:{label}")
    else:
        boxes = await _detect_boundaries(grid, key=f"title:{label}")
    if not boxes:
        boxes = [(0, len(grid) - 1, 0, max(width - 1, 0))]
    for box in boxes:
        subgrid = _slice_grid(grid, *box)
        sub_width = max((len(row) for row in subgrid), default=0)
        windowed = len(_typing_rows(subgrid, sub_width)) < len(subgrid)
        typed = await _type_rows(subgrid, key=f"title:{label}:{box[0]}", anomalies=_scan_anomalies(values, box))
        logger.info(
            "%-22s box=%-20s rows=%-5d path=%-6s windowed=%-5s headers=%-12s data_start=%-4s title=%r",
            label,
            str(box),
            len(subgrid),
            "large" if large else "small",
            windowed,
            None if typed is None else typed.header_rows,
            None if typed is None else typed.data_start,
            None if typed is None else typed.title,
        )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    blob = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()

    targets = []
    for sheet in load_all_sheets(blob):
        for index, region in enumerate(find_regions(sheet)):
            grid = region_grid(sheet, region)
            if len(grid) < 2:
                continue
            targets.append((f"sheet{sheet.sheet_no}_region{index}", grid, region_values(sheet, region)))

    await asyncio.gather(*starmap(_probe, targets))


if __name__ == "__main__":
    asyncio.run(main())
