import asyncio
import collections
import logging
import pathlib
import sys
from typing import TYPE_CHECKING

from citadel.services.excel import (
    Region,
    TableBounds,
    find_regions,
    load_all_sheets,
    region_grid,
)
from citadel.services.grid import classify_grid
from citadel.tabular.structure import merge_candidates, structure_candidate

if TYPE_CHECKING:
    from citadel.tabular.materialize import MaterializedTable

logger = logging.getLogger("merge")
SHEET_NO = 2


async def _run(sheet, candidates: list[tuple[Region, list[list[str]]]], attempt: int):
    resolved = await asyncio.gather(
        *(
            structure_candidate(
                grid,
                key=f"merge:{attempt}:sheet{sheet.sheet_no}:{index}",
                known_table=False,
                sheet_no=sheet.sheet_no,
                anchors={
                    "min_row": region.min_row,
                    "min_col": region.min_col,
                    "max_row": region.max_row,
                    "max_col": region.max_col,
                },
            )
            for index, (region, grid) in enumerate(candidates)
        )
    )
    bounds: list[TableBounds] = []
    members: list[MaterializedTable] = []
    for (region, _grid), tables in zip(candidates, resolved, strict=True):
        for table in tables:
            anchors = table.anchors
            bounds.append(
                TableBounds(
                    min_row=anchors.get("min_row", region.min_row),
                    min_col=anchors.get("min_col", region.min_col),
                    max_row=anchors.get("max_row", region.max_row),
                    max_col=anchors.get("max_col", region.max_col),
                )
            )
        members.extend(tables)
    merged = await merge_candidates(f"variance{attempt}", sheet.sheet_no, bounds, members)
    return bounds, members, merged


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    blob = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()
    sheet = next(item for item in load_all_sheets(blob) if item.sheet_no == SHEET_NO)

    candidates = []
    for region in find_regions(sheet):
        grid = region_grid(sheet, region)
        if classify_grid(grid) != "table":
            continue
        candidates.append((region, grid))
    logger.info("sheet%d table-candidate regions=%d", SHEET_NO, len(candidates))

    counts: collections.Counter[int] = collections.Counter()
    attempts = await asyncio.gather(*(_run(sheet, candidates, attempt) for attempt in range(runs)))
    for attempt, (bounds, members, merged) in enumerate(attempts):
        counts[len(merged)] += 1
        logger.info("--- run %d: candidates=%d merged=%d ---", attempt, len(members), len(merged))
        for index, (bound, member) in enumerate(zip(bounds, members, strict=True)):
            logger.info(
                "    cand[%d] rows=%d..%d cols=%d..%d n_rows=%d title=%r headers=%s",
                index,
                bound.min_row,
                bound.max_row,
                bound.min_col,
                bound.max_col,
                member.n_rows,
                member.title,
                [column.header for column in member.columns[:3]],
            )
        for index, table in enumerate(merged):
            logger.info("    merged[%d] n_rows=%d cols=%d", index, table.n_rows, len(table.columns))
    logger.info("counts over %d runs: %s", runs, dict(sorted(counts.items())))


if __name__ == "__main__":
    asyncio.run(main())
