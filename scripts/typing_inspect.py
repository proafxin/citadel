import asyncio
import json
import logging
import pathlib
import sys

from citadel.llm import call_structured, call_text
from citadel.prompts import load_prompt
from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.tabular.structure import (
    STAGE2_MAX_TOKENS,
    _TYPED_EXTRACT_SCHEMA,
    _render_excel,
    _typing_rows,
)

logger = logging.getLogger("typing")
DEFAULT = ("sheet1_region4", "sheet2_region3")


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
            rows = _typing_rows(grid, width)
            text = _render_excel(grid, rows, width)
            reasoning = await call_text(
                f"{load_prompt('table_structure_typed')}\n\n{text}",
                max_tokens=STAGE2_MAX_TOKENS,
                key=f"typing:{label}",
            )
            if len(rows) < len(grid):
                extract = f"{load_prompt('table_typed_extract')}\n\n{reasoning}"
            else:
                extract = f"{load_prompt('table_typed_extract_lines')}\n\n{text}\n\n{reasoning}"
            data = await call_structured(extract, _TYPED_EXTRACT_SCHEMA)
            logger.info("=== %s (%d rows) ===", label, len(grid))
            logger.info("--- reasoning ---\n%s", reasoning)
            logger.info("--- extracted ---\n%s\n", json.dumps(data, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
