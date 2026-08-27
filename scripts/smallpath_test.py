import asyncio
import pathlib

from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.tabular.structure import EXCEL_CHUNK_BUDGET, _chunk_rows, _detect_boundaries

REPEATS = 3


async def _probe(label: str, grid: list[list[str]], attempt: int) -> tuple[str, int, list]:
    boxes = await _detect_boundaries(grid, key=f"small:{label}:{attempt}")
    return label, attempt, boxes


async def main() -> None:
    path = "/home/masterkenway/Downloads/ocr_input/test(1).xlsx"
    blob = pathlib.Path(path).read_bytes()

    targets: list[tuple[str, int, list[list[str]]]] = []
    for sheet in load_all_sheets(blob):
        for index, region in enumerate(find_regions(sheet)):
            grid = region_grid(sheet, region)
            width = max((len(row) for row in grid), default=0)
            if len(_chunk_rows(grid, width, EXCEL_CHUNK_BUDGET)) > 1:
                continue
            targets.append((f"sheet{sheet.sheet_no}_region{index}", len(grid), grid))

    results = await asyncio.gather(
        *(_probe(label, grid, attempt) for label, _rows, grid in targets for attempt in range(REPEATS))
    )
    grouped: dict[str, list[tuple[int, list]]] = {}
    for label, attempt, boxes in results:
        grouped.setdefault(label, []).append((attempt, boxes))

    for label, _rows, _grid in targets:
        for attempt, boxes in sorted(grouped.get(label, [])):
            pass


if __name__ == "__main__":
    asyncio.run(main())
