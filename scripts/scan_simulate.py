import pathlib
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from citadel.llm import count_tokens
from citadel.services.excel import Cell, Region, SheetExtraction, find_regions, load_all_sheets

WINDOW_BUDGET = 8192
BREAK_CONTEXT = 2


@dataclass
class RowPrint:
    index: int
    values: dict[int, object] = field(default_factory=dict)
    types: dict[int, str] = field(default_factory=dict)
    formula_cols: frozenset[int] = frozenset()


def _native_class(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, datetime):
        return "temporal"
    return "string"


def _row_prints(region: Region) -> list[RowPrint]:
    by_row: dict[int, list[Cell]] = {}
    for cell in region.cells:
        by_row.setdefault(cell.row, []).append(cell)
    prints: list[RowPrint] = []
    for offset, sheet_row in enumerate(range(region.min_row, region.max_row + 1)):
        entry = RowPrint(index=offset)
        formulas: set[int] = set()
        for cell in by_row.get(sheet_row, []):
            col = cell.col - region.min_col
            if cell.formula:
                formulas.add(col)
            if cell.value is None:
                continue
            if isinstance(cell.value, str) and not cell.value.strip():
                continue
            entry.values[col] = cell.value
            entry.types[col] = _native_class(cell.value)
        entry.formula_cols = frozenset(formulas)
        prints.append(entry)
    return prints


def _column_types(population: list[RowPrint]) -> dict[int, str]:
    counts: dict[int, Counter[str]] = {}
    for entry in population:
        for col, cls in entry.types.items():
            counts.setdefault(col, Counter())[cls] += 1
    return {col: counter.most_common(1)[0][0] for col, counter in counts.items()}


def _conforms(entry: RowPrint, types: dict[int, str], extent: frozenset[int]) -> bool:
    if not entry.values:
        return False
    if not set(entry.values) <= extent:
        return False
    return all(types.get(col) == cls for col, cls in entry.types.items())


def _anchor(prints: list[RowPrint]) -> tuple[dict[int, str], frozenset[int]]:
    population = [entry for entry in prints if entry.values]
    types = _column_types(population)
    extent = frozenset(types)
    for _ in range(5):
        survivors = [entry for entry in population if _conforms(entry, types, extent)]
        if not survivors or len(survivors) == len(population):
            break
        population = survivors
        types = _column_types(population)
        extent = frozenset(types)
    return types, extent


def _render(entry: RowPrint, extent: frozenset[int]) -> str:
    width = max(extent) + 1 if extent else 0
    cells = [str(entry.values.get(col, "")) for col in range(width)]
    return f"row {entry.index}: " + " | ".join(cells)


def _window_rows(prints: list[RowPrint], extent: frozenset[int], start: int, budget: int) -> int:
    used = 0
    count = 0
    for entry in prints[start:]:
        cost = count_tokens(_render(entry, extent))
        if used + cost > budget and count:
            break
        used += cost
        count += 1
    return count


def _merge_windows(breaks: list[int], total: int) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    for index in breaks:
        lo = max(index - BREAK_CONTEXT, 0)
        hi = min(index + BREAK_CONTEXT, total - 1)
        if windows and lo <= windows[-1][1] + 1:
            windows[-1] = (windows[-1][0], max(windows[-1][1], hi))
            continue
        windows.append((lo, hi))
    return windows


def _tokens(prints: list[RowPrint], extent: frozenset[int], lo: int, hi: int) -> int:
    return sum(count_tokens(_render(entry, extent)) for entry in prints[lo : hi + 1])


def _label(sheet: SheetExtraction, index: int, region: Region) -> str:
    return f"sheet{sheet.sheet_no}_region{index}_r{region.min_row}-{region.max_row}_c{region.min_col}-{region.max_col}"


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/home/masterkenway/Downloads/ocr_input/test(1).xlsx"
    data = pathlib.Path(path).read_bytes()
    sheets = load_all_sheets(data)

    grand_old = 0
    grand_new = 0
    grand_calls = 0

    for sheet in sheets:
        for _index, region in enumerate(find_regions(sheet)):
            prints = _row_prints(region)
            types, extent = _anchor(prints)
            first_data = next((entry.index for entry in prints if _conforms(entry, types, extent)), 0)
            breaks = [
                entry.index for entry in prints if entry.index > first_data and not _conforms(entry, types, extent)
            ]

            anchor_count = _window_rows(prints, extent, 0, WINDOW_BUDGET)
            anchor_tokens = _tokens(prints, extent, 0, anchor_count - 1)
            windows = [(lo, hi) for lo, hi in _merge_windows(breaks, len(prints)) if lo >= anchor_count]
            break_tokens = sum(_tokens(prints, extent, lo, hi) for lo, hi in windows)

            old = _tokens(prints, extent, 0, len(prints) - 1)
            new = anchor_tokens + break_tokens
            calls = 1 + len(windows)
            grand_old += old
            grand_new += new
            grand_calls += calls

            f"{new / old:.2%}" if old else "n/a"

    f"{grand_new / grand_old:.2%}" if grand_old else "n/a"


if __name__ == "__main__":
    main()
