import asyncio
import pathlib
import sys
from collections import Counter

from scan_simulate import RowPrint, _anchor, _row_prints
from scan_simulate import _conforms as _sim_conforms

from citadel.llm import count_tokens
from citadel.services.excel import find_regions, load_all_sheets, region_grid
from citadel.tabular.structure import EXCEL_CHUNK_BUDGET, _detect_boundaries_chunk, _render_excel, _row_line_excel

BREAK_CONTEXT = 4


def _verify(
    grid: list[list[str]],
    prints: list[RowPrint],
    tables: list[tuple[int, int, int, dict[int, str], frozenset[int]]],
    unexplained: list[int],
) -> list[str]:
    rows = len(grid)
    problems: list[str] = []
    ordered = sorted(tables)
    spans: list[tuple[int, int]] = []
    for position, (start, _c0, _c1, _types, _extent) in enumerate(ordered):
        end = ordered[position + 1][0] - 1 if position + 1 < len(ordered) else rows - 1
        if end < start:
            problems.append(f"table at {start} has empty span (end={end})")
        spans.append((start, end))

    covered: set[int] = set()
    for start, end in spans:
        for row in range(start, end + 1):
            if row in covered:
                problems.append(f"row {row} claimed by two tables")
            covered.add(row)

    uncovered = sorted(set(range(rows)) - covered)
    if spans:
        stray = [row for row in uncovered if row > spans[0][0]]
        if stray:
            problems.append(f"rows after first table start left uncovered: {stray[:10]}")

    for start, _c0, _c1, types, extent in ordered:
        lost = [row for row in uncovered if row < start and _conforms(prints[row], types, extent)]
        if lost:
            problems.append(f"pre-anchor rows conform to table at {start} (possible data loss): {lost}")

    problems.extend(f"unexplained break {row} falls outside every table" for row in unexplained if row not in covered)

    headers = {start for start, _c0, _c1, _types, _extent in ordered}
    sim_types, sim_extent = _anchor(prints)
    independent = [entry.index for entry in prints if entry.values and not _sim_conforms(entry, sim_types, sim_extent)]
    reported = set(unexplained) | set(uncovered) | headers
    missed = [row for row in independent if row not in reported]
    [row for row in sorted(reported) if row not in independent]
    if missed:
        problems.append(f"independent scan flags rows the large path treated as data: {missed[:10]}")
    return problems


def _window(grid: list[list[str]], start: int, budget: int) -> tuple[str, int]:
    width = max((len(row) for row in grid), default=0)
    rows: list[int] = []
    used = 0
    for index in range(start, len(grid)):
        cost = count_tokens(_row_line_excel(index, grid[index], width))
        if used + cost > budget and rows:
            break
        rows.append(index)
        used += cost
    return _render_excel(grid, rows, width), rows[-1]


def _profile(prints: list[RowPrint], lo: int, hi: int) -> dict[int, str]:
    counts: dict[int, Counter[str]] = {}
    for entry in prints[lo : hi + 1]:
        for col, cls in entry.types.items():
            counts.setdefault(col, Counter())[cls] += 1
    return {col: counter.most_common(1)[0][0] for col, counter in counts.items()}


def _conforms(entry: RowPrint, types: dict[int, str], extent: frozenset[int]) -> bool:
    if not entry.values:
        return False
    if not set(entry.values) <= extent:
        return False
    return all(types.get(col) in {None, cls} for col, cls in entry.types.items())


def _scan(prints: list[RowPrint], types: dict[int, str], extent: frozenset[int], start: int) -> list[int]:
    return [entry.index for entry in prints[start:] if not _conforms(entry, types, extent)]


async def main() -> None:
    path = "/home/masterkenway/Downloads/ocr_input/test(1).xlsx"
    target = sys.argv[1] if len(sys.argv) > 1 else "sheet1_region9"

    blob = pathlib.Path(path).read_bytes()

    for sheet in load_all_sheets(blob):
        for index, region in enumerate(find_regions(sheet)):
            label = f"sheet{sheet.sheet_no}_region{index}"
            if label != target:
                continue
            grid = region_grid(sheet, region)
            prints = _row_prints(region)
            for probe in [*range(6), len(grid) - 3, len(grid) - 2, len(grid) - 1]:
                prints[probe]

            queue = [(0, 0)]
            seen: set[int] = set()
            calls = 0
            sent = 0
            tables: list[tuple[int, int, int, dict[int, str], frozenset[int]]] = []
            unexplained: list[int] = []
            while queue:
                batch = [(win, claim) for win, claim in queue if claim not in seen]
                seen.update(claim for _win, claim in batch)
                queue = []
                rendered = [_window(grid, win, EXCEL_CHUNK_BUDGET) for win, _claim in batch]
                for (_win, claim), (text, _last) in zip(batch, rendered, strict=True):
                    sent += count_tokens(text)
                results = await asyncio.gather(
                    *(
                        _detect_boundaries_chunk(text, key=f"large:{label}:{claim}")
                        for (_win, claim), (text, _l) in zip(batch, rendered, strict=True)
                    )
                )
                calls += len(batch) * 2
                for (_win, claim), (_text, last), boxes in zip(batch, rendered, results, strict=True):
                    accepted = False
                    for start_row, _end_row, c0, c1 in boxes:
                        if start_row < claim:
                            continue
                        accepted = True
                        data_lo = min(start_row + 1, last)
                        extent = frozenset(range(c0, c1 + 1))
                        types = _profile(prints, data_lo, last)
                        breaks = _scan(prints, types, extent, last + 1)
                        tables.append((start_row, c0, c1, types, extent))
                        if breaks:
                            queue.append((max(breaks[0] - BREAK_CONTEXT, 0), breaks[0]))
                    if not accepted and claim > 0:
                        unexplained.append(claim)

            problems = _verify(grid, prints, tables, unexplained)
            if problems:
                for _problem in problems:
                    pass
            return


if __name__ == "__main__":
    asyncio.run(main())
