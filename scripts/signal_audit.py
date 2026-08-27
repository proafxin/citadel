import logging
import pathlib
from collections import Counter

from citadel.services.excel import find_regions, load_all_sheets

logger = logging.getLogger("signals")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    blob = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()
    sheets = load_all_sheets(blob)

    totals: Counter[str] = Counter()
    for sheet in sheets:
        for cell in sheet.cells:
            totals["cells"] += 1
            totals["bold"] += bool(cell.bold)
            totals["filled"] += bool(cell.filled)
            totals["bordered"] += bool(cell.bordered)
            totals["formatted"] += cell.number_format not in {"General", None}
            totals["formula"] += bool(cell.formula)
            totals["comment"] += bool(cell.comment)
    logger.info("file totals: %s", dict(totals))

    for sheet in sheets:
        for index, region in enumerate(find_regions(sheet)):
            by_row: dict[int, Counter[str]] = {}
            for cell in region.cells:
                bucket = by_row.setdefault(cell.row - region.min_row, Counter())
                bucket["cells"] += 1
                bucket["bold"] += bool(cell.bold)
                bucket["filled"] += bool(cell.filled)
                bucket["bordered"] += bool(cell.bordered)
                bucket["formatted"] += cell.number_format not in {"General", None}
            marked = [row for row, counts in by_row.items() if counts["bold"] or counts["filled"] or counts["bordered"]]
            if not marked:
                continue
            logger.info("sheet%d_region%d rows=%d styled_rows=%s", sheet.sheet_no, index, len(by_row), marked[:12])
            for row in marked[:4]:
                logger.info("    row %d: %s", row, dict(by_row[row]))


if __name__ == "__main__":
    main()
