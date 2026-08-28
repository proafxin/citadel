import asyncio
import json
import pathlib
import sys

from citadel.llm import call_structured, call_text
from citadel.prompts import load_prompt
from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.services.grid import classify_grid
from citadel.tabular.structure import _EXTRACT_ALL_SCHEMA, STAGE2_MAX_TOKENS, _structure_render

MAX_TOKENS = 3072

PROMPT = (
    "Below are the table structures extracted from one sheet of an excel workbook, in the order they appear "
    "in the sheet. Each was extracted from its own region of the sheet, independently of the others. "
    "Give the rundown of the tables that are actually in this sheet."
)


async def _region_structure(key: str, grid: list[list[str]]) -> list[dict]:
    text, _rendered = _structure_render(grid)
    description = await call_text(
        f"{load_prompt('table_structure_typed')}\n\n{text}", max_tokens=STAGE2_MAX_TOKENS, key=key
    )
    data = await call_structured(f"{load_prompt('table_extract_all')}\n\n{text}\n\n{description}", _EXTRACT_ALL_SCHEMA)
    return data.get("tables") or []


async def main() -> None:
    out_dir = pathlib.Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    path = pathlib.Path(sys.argv[2])
    wanted = {int(arg) for arg in sys.argv[3:]}

    parts = [f"# rundown probe — {path.name}"]
    for sheet in load_all_sheets(path.read_bytes()):
        if wanted and sheet.sheet_no not in wanted:
            continue
        jobs = []
        positions = []
        for index, region in enumerate(find_regions(sheet)):
            grid = region_grid(sheet, region)
            if classify_grid(grid) != "table":
                continue
            positions.append((index, region))
            jobs.append(_region_structure(f"rundown:sheet{sheet.sheet_no}:{index}", grid))
        if not jobs:
            continue
        results = await asyncio.gather(*jobs)
        extracted = [
            {
                "region": index,
                "sheet_rows": [region.min_row, region.max_row],
                "sheet_cols": [region.min_col, region.max_col],
                "tables": tables,
            }
            for (index, region), tables in zip(positions, results, strict=True)
        ]
        listing = json.dumps(extracted, indent=2)
        answer = await call_text(f"{PROMPT}\n\n{listing}", max_tokens=MAX_TOKENS, key=f"rundown:{sheet.sheet_no}")
        parts.append(
            f"\n## sheet{sheet.sheet_no} {sheet.sheet_name!r}"
            f"\n\n### sent\n\n```json\n{listing}\n```\n\n### model output\n\n{answer}"
        )
    (out_dir / f"{path.stem.replace(' ', '_')}_rundown.md").write_text("\n".join(parts) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
