import asyncio
import dataclasses
import json
import pathlib
import sys

from citadel.services.excel import (
    Region,
    SheetExtraction,
    find_regions,
    load_all_sheets,
    region_bold,
    region_grid,
    region_values,
)
from citadel.services.grid import classify_grid
from citadel.tabular.materialize import MaterializedTable
from citadel.tabular.structure import _bold_note, _row_line, _structure_render, structure_candidate

SHOW_ROWS = 4


async def _region(
    sheet: SheetExtraction, index: int, region: Region, grid: list[list[str]]
) -> tuple[int, list[MaterializedTable]]:
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
        values=region_values(sheet, region),
        bold=region_bold(sheet, region),
    )
    return index, tables


def _content(grid: list[list[str]], bold: list[list[bool]]) -> str:
    width = max((len(row) for row in grid), default=0)
    lines = [_row_line(index, row, width, None, _bold_note(bold, index)) for index, row in enumerate(grid)]
    return f"{len(grid)} rows, {width} cols\n" + "\n".join(lines)


def _render_table(table: MaterializedTable) -> str:
    fields = {key: value for key, value in dataclasses.asdict(table).items() if key != "rows"}
    body = json.dumps(fields, indent=2, default=str)
    rows = "\n".join(" | ".join(str(cell) for cell in row) for row in table.rows[:SHOW_ROWS])
    return f"```json\n{body}\n```\n\nfirst rows:\n\n```\n{rows}\n```"


def _section(
    index: int,
    region: Region,
    grid: list[list[str]],
    bold: list[list[bool]],
    kind: str,
    tables: list[MaterializedTable] | None,
) -> str:
    head = (
        f"\n### region{index} — sheet rows {region.min_row}-{region.max_row}, "
        f"cols {region.min_col}-{region.max_col} — {kind}"
    )
    parts = [f"{head}\n\n**region content**\n\n```\n{_content(grid, bold)}\n```"]
    if tables is None:
        parts.append("\n**NOT STRUCTURED**")
        return "\n".join(parts)
    window, last = _structure_render(grid, 0, bold)
    parts.append(f"\n**model window (rows 0-{last} of {len(grid)})**\n\n```\n{window}\n```")
    parts.append(f"\n**detected {len(tables)} table(s)**")
    parts.extend(f"\n#### table {position}\n\n{_render_table(table)}" for position, table in enumerate(tables))
    return "\n".join(parts)


async def main() -> None:
    out_dir = pathlib.Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    for arg in sys.argv[2:]:
        path = pathlib.Path(arg)
        stem = path.stem.replace(" ", "_")
        for sheet in load_all_sheets(path.read_bytes()):
            regions = find_regions(sheet)
            grids = [region_grid(sheet, region) for region in regions]
            kinds = [classify_grid(grid) for grid in grids]
            jobs = [
                _region(sheet, index, region, grid)
                for index, (region, grid, kind) in enumerate(zip(regions, grids, kinds, strict=True))
                if kind == "table"
            ]
            structured = dict(await asyncio.gather(*jobs))
            parts = [f"# {path.name} — sheet{sheet.sheet_no} {sheet.sheet_name!r} — {len(regions)} regions"]
            parts.extend(
                _section(index, region, grid, region_bold(sheet, region), kind, structured.get(index))
                for index, (region, grid, kind) in enumerate(zip(regions, grids, kinds, strict=True))
            )
            (out_dir / f"{stem}_sheet{sheet.sheet_no}.md").write_text("\n".join(parts) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
