import asyncio
import logging
import pathlib

from citadel.services.excel import find_regions, load_all_sheets, region_grid, region_values
from citadel.tabular.structure import (
    _anchor_boundaries,
    _apply_scan,
    _combine_structure,
    _scan_anomalies,
    _slice_grid,
    _type_rows,
)

logger = logging.getLogger("region9")
TARGET = ("sheet1_region9",)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    blob = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()

    for sheet in load_all_sheets(blob):
        for index, region in enumerate(find_regions(sheet)):
            label = f"sheet{sheet.sheet_no}_region{index}"
            if label not in TARGET:
                continue
            grid = region_grid(sheet, region)
            values = region_values(sheet, region)
            logger.info("%s region rows=%d", label, len(grid))
            boxes, scanned = await _anchor_boundaries(grid, values, key=f"probe:{label}")
            logger.info("boxes=%s scanned=%s", boxes, scanned[:20])
            for box in boxes:
                subgrid = _slice_grid(grid, *box)
                anomalies = _scan_anomalies(values, box)
                typed = await _type_rows(subgrid, key=f"probe:{label}:{box[0]}", anomalies=anomalies)
                logger.info("box=%s subgrid_rows=%d anomalies=%s", box, len(subgrid), anomalies[:20])
                logger.info("  typed=%s", typed)
                structure = _combine_structure(grid, box, typed, len(subgrid))
                logger.info("  combined=%s", structure)
                if scanned and structure is not None:
                    logger.info("  after_scan=%s", _apply_scan(structure, box, scanned, len(subgrid)))


if __name__ == "__main__":
    asyncio.run(main())
