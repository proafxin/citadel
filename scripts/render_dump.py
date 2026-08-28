import pathlib
import sys

from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.services.grid import classify_grid, grid_text
from citadel.tabular.structure import _structure_render


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
                    parts.append(f"\n### region{index} — {kind}, not structured\n\n```\n{grid_text(grid)}\n```")
                    continue
                text, rendered = _structure_render(grid)
                parts.append(
                    f"\n### region{index} — rows {len(grid)}, rendered {len(rendered)}\n\n```\n{text}\n```"
                )
        (out_dir / f"{path.stem.replace(' ', '_')}.md").write_text("\n".join(parts) + "\n")


if __name__ == "__main__":
    main()
