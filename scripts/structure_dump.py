import asyncio
import json
import pathlib
import sys

from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.services.grid import classify_grid, grid_text
from citadel.tabular.materialize import MaterializedTable
from citadel.tabular.structure import structure_candidate

SHOW_ROWS = 4


async def _region(sheet, index: int, region, grid: list[list[str]]) -> tuple[int, list[MaterializedTable]]:
    tables = await structure_candidate(
        grid,
        key=f"dump:sheet{sheet.sheet_no}:{index}",
        known_table=False,
        sheet_no=sheet.sheet_no,
        anchors={
            "min_row": region.min_row,
            "min_col": region.min_col,
            "max_row": region.max_row,
            "max_col": region.max_col,
        },
    )
    return index, tables


def _render_table(table: MaterializedTable) -> str:
    body = json.dumps(table.model_dump(exclude={"rows"}), indent=2, default=str)
    rows = "\n".join(" | ".join(str(cell) for cell in row) for row in table.rows[:SHOW_ROWS])
    return f"```json\n{body}\n```\n\nfirst rows:\n\n```\n{rows}\n```"


async def main() -> None:
    out_dir = pathlib.Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    for arg in sys.argv[2:]:
        path = pathlib.Path(arg)
        parts = [f"# {path.name}"]
        for sheet in load_all_sheets(path.read_bytes()):
            parts.append(f"\n## sheet{sheet.sheet_no} {sheet.sheet_name!r}")
            jobs = []
            skipped = []
            for index, region in enumerate(find_regions(sheet)):
                grid = region_grid(sheet, region)
                if classify_grid(grid) != "table":
                    skipped.append((index, region, classify_grid(grid), grid_text(grid)))
                    continue
                jobs.append(_region(sheet, index, region, grid))
            for index, region, kind, body in skipped:
                parts.append(
                    f"\n### region{index} — rows {region.min_row}-{region.max_row}, "
                    f"cols {region.min_col}-{region.max_col} — {kind}, NOT STRUCTURED\n\n```\n{body}\n```"
                )
            for index, tables in await asyncio.gather(*jobs):
                region = find_regions(sheet)[index]
                head = (
                    f"\n### region{index} — rows {region.min_row}-{region.max_row}, "
                    f"cols {region.min_col}-{region.max_col} — {len(tables)} table(s)"
                )
                parts.append(head)
                parts.extend(f"\n#### table {position}\n\n{_render_table(table)}" for position, table in enumerate(tables))
        (out_dir / f"{path.stem.replace(' ', '_')}_structures.md").write_text("\n".join(parts) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
