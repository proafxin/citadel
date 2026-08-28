import asyncio
import collections
import logging
import pathlib
import sys

from citadel.llm import call_structured, call_text
from citadel.prompts import load_prompt
from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.tabular.structure import SINGLE_TABLE_BUDGET, STAGE2_MAX_TOKENS, _candidate_text, _row_line

logger = logging.getLogger("live")
PATH = "/home/masterkenway/Downloads/ocr_input/test(1).xlsx"
DEFAULT = ("sheet1_region3", "sheet1_region9", "sheet1_region6", "sheet2_region1")

_TABLE = {
    "type": "object",
    "properties": {
        "start_row": {"type": "integer"},
        "end_row": {"type": "integer"},
        "start_col": {"type": "integer"},
        "end_col": {"type": "integer"},
        "header_rows": {"type": "array", "items": {"type": "integer"}},
        "data_start": {"type": "integer"},
        "data_end": {"type": "integer"},
        "metadata_rows": {"type": "array", "items": {"type": "integer"}},
        "title_row": {"type": ["integer", "null"]},
    },
    "required": [
        "start_row",
        "end_row",
        "start_col",
        "end_col",
        "header_rows",
        "data_start",
        "data_end",
        "metadata_rows",
        "title_row",
    ],
}
_SCHEMA = {
    "type": "object",
    "properties": {"tables": {"type": "array", "items": _TABLE}},
    "required": ["tables"],
}


def _render(grid: list[list[str]]) -> str:
    width = max((len(row) for row in grid), default=0)
    cells = sum(1 for row in grid for cell in row if cell.strip())
    if cells > 400:
        return _candidate_text(grid, full=False, budget=SINGLE_TABLE_BUDGET)
    lines = [_row_line(index, row, width, None) for index, row in enumerate(grid)]
    return f"{len(grid)} rows, {width} cols\n" + "\n".join(lines)


async def _attempt(label: str, grid: list[list[str]], attempt: int) -> tuple[str, int, list[dict]]:
    text = _render(grid)
    description = await call_text(
        f"{load_prompt('table_structure_typed')}\n\n{text}",
        max_tokens=STAGE2_MAX_TOKENS,
        key=f"live:{label}:{attempt}",
    )
    data = await call_structured(f"{load_prompt('table_extract_all')}\n\n{text}\n\n{description}", _SCHEMA)
    return label, attempt, data.get("tables") or []


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    targets = tuple(sys.argv[2:]) or DEFAULT
    blob = pathlib.Path(PATH).read_bytes()

    selected: list[tuple[str, list[list[str]]]] = []
    for sheet in load_all_sheets(blob):
        for index, region in enumerate(find_regions(sheet)):
            label = f"sheet{sheet.sheet_no}_region{index}"
            if label in targets:
                selected.append((label, region_grid(sheet, region)))

    jobs = [_attempt(label, grid, attempt) for label, grid in selected for attempt in range(runs)]
    grouped: dict[str, list[tuple[int, list[dict]]]] = {}
    for label, attempt, tables in await asyncio.gather(*jobs):
        grouped.setdefault(label, []).append((attempt, tables))

    for label, _grid in selected:
        counts: collections.Counter[int] = collections.Counter()
        logger.info("=== %s ===", label)
        for attempt, tables in sorted(grouped[label]):
            counts[len(tables)] += 1
            logger.info("  attempt=%d n=%d", attempt, len(tables))
            for table in tables:
                logger.info(
                    "     box=(%s,%s,%s,%s) headers=%s data=%s..%s meta=%s title_row=%s",
                    table.get("start_row"),
                    table.get("end_row"),
                    table.get("start_col"),
                    table.get("end_col"),
                    table.get("header_rows"),
                    table.get("data_start"),
                    table.get("data_end"),
                    table.get("metadata_rows"),
                    table.get("title_row"),
                )
        logger.info("  table-count distribution: %s", dict(sorted(counts.items())))


if __name__ == "__main__":
    asyncio.run(main())
