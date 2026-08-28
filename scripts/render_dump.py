import pathlib
import sys

from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.services.grid import classify_grid, grid_text
from citadel.tabular.structure import _row_line

HEAD_ROWS = 40
TAIL_ROWS = 15


def _full(grid: list[list[str]]) -> str:
    width = max((len(row) for row in grid), default=0)
    lines = [_row_line(index, row, width, None) for index, row in enumerate(grid)]
    if len(lines) <= HEAD_ROWS + TAIL_ROWS:
        body = "\n".join(lines)
    else:
        skipped = len(lines) - HEAD_ROWS - TAIL_ROWS
        body = "\n".join([*lines[:HEAD_ROWS], f"... {skipped} rows omitted ...", *lines[-TAIL_ROWS:]])
    return f"{len(grid)} rows, {width} cols\n{body}"


def main() -> None:
    out_dir = pathlib.Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    for arg in sys.argv[2:]:
        path = pathlib.Path(arg)
        parts = [f"# {path.name}"]
        for sheet in load_all_sheets(path.read_bytes()):
            parts.append(f"\n## sheet{sheet.sheet_no} {sheet.sheet_name!r}")
            for index, region in enumerate(find_regions(sheet)):
                grid = region_grid(sheet, region)
                kind = classify_grid(grid)
                if kind != "table":
                    parts.append(
                        f"\n### region{index} — sheet rows {region.min_row}-{region.max_row}, "
                        f"cols {region.min_col}-{region.max_col} — {kind}, not structured"
                        f"\n\n```\n{grid_text(grid)}\n```"
                    )
                    continue
                parts.append(
                    f"\n### region{index} — sheet rows {region.min_row}-{region.max_row}, "
                    f"cols {region.min_col}-{region.max_col}\n\n```\n{_full(grid)}\n```"
                )
        (out_dir / f"{path.stem.replace(' ', '_')}.md").write_text("\n".join(parts) + "\n")


if __name__ == "__main__":
    main()
