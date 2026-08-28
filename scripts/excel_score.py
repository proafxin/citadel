import asyncio
import json
import logging
import pathlib
import sys

from citadel.services.excel import (
    SheetExtraction,
    find_regions,
    load_all_sheets,
    region_grid,
    region_values,
)
from citadel.services.grid import classify_grid
from citadel.tabular.materialize import MaterializedTable
from citadel.tabular.structure import structure_candidate

logger = logging.getLogger("score")

TRUTH = pathlib.Path("data/model_baselines/excel_ground_truth.json")
SOURCES = {
    "1.xlsx": "/home/masterkenway/Downloads/ocr_input/1.xlsx",
    "2.xlsx": "/home/masterkenway/Downloads/ocr_input/2.xlsx",
}


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]) + 1)


def _score_pair(expected: dict, table: MaterializedTable) -> int:
    anchors = table.anchors
    rows = (anchors.get("min_row", 0), anchors.get("max_row", 0))
    cols = (anchors.get("min_col", 0), anchors.get("max_col", 0))
    return _overlap(rows, tuple(expected["row_span"])) * _overlap(cols, tuple(expected["col_span"]))


async def _sheet_tables(sheet: SheetExtraction) -> list[MaterializedTable]:
    jobs = []
    for index, region in enumerate(find_regions(sheet)):
        grid = region_grid(sheet, region)
        if classify_grid(grid) != "table":
            continue
        jobs.append(
            structure_candidate(
                grid,
                key=f"score:sheet{sheet.sheet_no}:{index}",
                known_table=False,
                sheet_no=sheet.sheet_no,
                anchors={
                    "min_row": region.min_row,
                    "min_col": region.min_col,
                    "max_row": region.max_row,
                    "max_col": region.max_col,
                },
                values=region_values(sheet, region),
            )
        )
    return [table for tables in await asyncio.gather(*jobs) for table in tables]


def _report(expected: list[dict], produced: list[MaterializedTable]) -> tuple[int, int, int, int]:
    unclaimed = list(range(len(produced)))
    matched = missed = wrong_rows = 0
    for want in expected:
        best, best_score = None, 0
        for position in unclaimed:
            score = _score_pair(want, produced[position])
            if score > best_score:
                best, best_score = position, score
        if best is None:
            logger.info("    MISSED   %-22s rows %s cols %s", want["id"], want["row_span"], want["col_span"])
            missed += 1
            continue
        unclaimed.remove(best)
        table = produced[best]
        matched += 1
        want_rows = want.get("rows")
        if want_rows is not None and table.n_rows != want_rows:
            logger.info(
                "    ROWS     %-22s expected %-5s got %-5s  headers=%s",
                want["id"],
                want_rows,
                table.n_rows,
                [column.header for column in table.columns[:3]],
            )
            wrong_rows += 1
        else:
            logger.info("    ok       %-22s rows %s", want["id"], table.n_rows)
    for position in unclaimed:
        table = produced[position]
        anchors = table.anchors
        logger.info(
            "    INVENTED rows %s-%s cols %s-%s n_rows=%d headers=%s",
            anchors.get("min_row"),
            anchors.get("max_row"),
            anchors.get("min_col"),
            anchors.get("max_col"),
            table.n_rows,
            [column.header for column in table.columns[:3]],
        )
    return matched, missed, wrong_rows, len(unclaimed)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    books = sys.argv[1:] or list(SOURCES)
    totals = [0, 0, 0, 0]
    for book in books:
        blob = pathlib.Path(SOURCES[book]).read_bytes()
        logger.info("########## %s ##########", book)
        for sheet in load_all_sheets(blob):
            expected = truth[book].get(str(sheet.sheet_no), [])
            produced = await _sheet_tables(sheet)
            if not expected and not produced:
                continue
            logger.info("  sheet%d — expected %d, produced %d", sheet.sheet_no, len(expected), len(produced))
            counts = _report(expected, produced)
            totals = [total + count for total, count in zip(totals, counts, strict=True)]
    logger.info(
        "\nTOTAL matched=%d missed=%d wrong_rows=%d invented=%d",
        totals[0],
        totals[1],
        totals[2],
        totals[3],
    )


if __name__ == "__main__":
    asyncio.run(main())
