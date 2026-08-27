import asyncio
import pathlib
import time

from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.tabular.structure import _type_rows

OUT_DIR = "/home/masterkenway/Projects/citadel/scripts/typed_output"
REPEATS = 5


async def run_case(label: str, grid: list[list[str]]) -> None:
    for attempt in range(REPEATS):
        start = time.monotonic()
        structure = await _type_rows(grid, key=f"typedtest:{label}:{attempt}")
        time.monotonic() - start
        out_path = f"{OUT_DIR}/{label}_attempt{attempt}.txt"
        pathlib.Path(out_path).write_text(f"structure={structure}\n", encoding="utf-8")


async def main() -> None:
    pathlib.Path(OUT_DIR).mkdir(exist_ok=True, parents=True)

    data = pathlib.Path("/home/masterkenway/Downloads/ocr_input/test(1).xlsx").read_bytes()
    sheets = load_all_sheets(data)

    sheet1 = next(s for s in sheets if s.sheet_no == 1)
    regions = find_regions(sheet1)

    region2_grid = region_grid(sheet1, regions[2])
    region4_grid = region_grid(sheet1, regions[4])
    region9_grid = region_grid(sheet1, regions[9])
    region9_tail = region9_grid[-30:]

    await run_case("region2", region2_grid)
    await run_case("region4", region4_grid)
    await run_case("region9_tail", region9_tail)


asyncio.run(main())
