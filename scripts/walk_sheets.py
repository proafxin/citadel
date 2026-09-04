import asyncio
import logging
import sys
from pathlib import Path

from citadel.services.excel import SheetExtraction, load_all_sheets, render_sheet_dump
from citadel.tabular.sheet_dump import parse_dump
from citadel.tabular.sheet_structure import RowRole, coverage, rows_by_role
from citadel.tabular.sheet_walk import walk_sheet, walk_structure
from config import configure_logging

logger = logging.getLogger(__name__)

UNSAFE = str.maketrans({"/": "_", "\\": "_", " ": "_", ":": "_", "*": "_", "?": "_", "[": "_", "]": "_", '"': "_"})


async def run_sheet(sheet: SheetExtraction, workbook: str, out_dir: Path) -> None:
    text = render_sheet_dump(sheet, workbook)
    dump = parse_dump(text)
    if not dump.last_row:
        return
    result = await walk_sheet(dump)
    structure = walk_structure(dump, result)
    name = f"{Path(workbook).stem}__{sheet.sheet_no:02d}_{sheet.sheet_name.translate(UNSAFE)}"
    (out_dir / f"{name}.json").write_text(structure.model_dump_json(indent=2), encoding="utf-8")
    logger.info(
        "%s/%s -> %d regions, %d metadata, coverage=%.3f, %d calls, %d free rows, %d unclaimed cells",
        workbook,
        sheet.sheet_name,
        len(structure.regions),
        len(structure.metadata),
        coverage(dump, structure),
        result.calls,
        result.extended,
        len(result.unclaimed),
    )
    for region in structure.regions:
        body = len(rows_by_role(region, RowRole.BODY))
        logger.info(
            "    %-4s rows %4d-%-4d cols %3d-%-3d body=%3d header=%s",
            region.region_id,
            region.first_row,
            region.last_row,
            region.first_col,
            region.last_col,
            body,
            rows_by_role(region, RowRole.HEADER),
        )


def kept(results: list[object], label: str) -> None:
    for item in results:
        if isinstance(item, BaseException):
            logger.error("%s failed: %s: %s", label, type(item).__name__, item)


async def run_workbook(path: Path, out_dir: Path) -> None:
    sheets = load_all_sheets(path.read_bytes())
    results = await asyncio.gather(
        *(run_sheet(sheet, path.name, out_dir) for sheet in sheets), return_exceptions=True
    )
    kept(list(results), path.name)


async def main() -> None:
    configure_logging()
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    results = await asyncio.gather(
        *(run_workbook(Path(raw), out_dir) for raw in sys.argv[2:]), return_exceptions=True
    )
    kept(list(results), "workbook")


if __name__ == "__main__":
    asyncio.run(main())
