import asyncio
import logging
import pathlib
import time

from citadel.services.excel import extract_sheet_content, load_all_sheets
from citadel.tabular.materialize import MaterializedTable

logger = logging.getLogger("sheets")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    blob = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()

    for sheet in load_all_sheets(blob):
        start = time.monotonic()
        items = await extract_sheet_content("verify", sheet)
        elapsed = time.monotonic() - start
        tables = [item for _position, item in items if isinstance(item, MaterializedTable)]
        texts = [item for _position, item in items if not isinstance(item, MaterializedTable)]
        logger.info(
            "sheet%d items=%d tables=%d text_blocks=%d elapsed=%.1fs",
            sheet.sheet_no,
            len(items),
            len(tables),
            len(texts),
            elapsed,
        )
        for position, table in enumerate(tables):
            logger.info(
                "    table[%d] n_rows=%d cols=%d header_rows=%s metadata=%s headers=%s",
                position,
                table.n_rows,
                len(table.columns),
                table.header_rows,
                table.anchors.get("metadata_rows"),
                [column.header for column in table.columns[:5]],
            )


if __name__ == "__main__":
    asyncio.run(main())
