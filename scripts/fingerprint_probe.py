import pathlib
import re
import sys
from collections import Counter
from dataclasses import dataclass, field

from citadel.schemas.table import ColumnDType
from citadel.services.excel import Cell, Region, SheetExtraction, cell_value, find_regions, load_all_sheets
from citadel.tabular.materialize import dtype_of

_REF = re.compile(
    r"(?:(?:'[^']+'|[A-Za-z_][A-Za-z0-9_.]*)!)?\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?",
)
_NUMERIC = {ColumnDType.INTEGER, ColumnDType.DECIMAL, ColumnDType.FLOAT}
_TEMPORAL = {ColumnDType.DATE, ColumnDType.DATETIME}
_SENTINEL_MAX_LEN = 12
_SENTINEL_MIN_REPEAT = 2
_NUMERIC_SHARE = 0.6
_CORE_SHARE = 0.8
_MIXED_SHARE = 0.3
_RARE_MAX_ROWS = 1
_MIN_PROFILE_ROWS = 12
_KEY_OVERLAP_MIN = 0.5
_EXTENT_MIN_COLS = 2


@dataclass
class RowPrint:
    index: int
    sheet_row: int
    values: dict[int, str] = field(default_factory=dict)
    types: dict[int, str] = field(default_factory=dict)
    max_len: int = 0
    spanning_formula: bool = False
    formula_cols: frozenset[int] = frozenset()


@dataclass
class Profile:
    types: dict[int, str] = field(default_factory=dict)
    core: frozenset[int] = frozenset()
    median_arity: int = 0
    mixed: frozenset[int] = frozenset()
    rare: frozenset[int] = frozenset()
    sentinels: dict[int, frozenset[str]] = field(default_factory=dict)


def _type_class(dtype: ColumnDType) -> str:
    if dtype in _NUMERIC:
        return "number"
    if dtype in _TEMPORAL:
        return "temporal"
    return dtype.value


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _formula_spans_rows(formula: str, own_row: int) -> bool:
    for match in _REF.finditer(formula):
        start_row = int(match.group(2))
        end_row = int(match.group(4)) if match.group(4) else start_row
        if start_row != own_row or end_row != own_row:
            return True
    return False


def _row_prints(region: Region) -> list[RowPrint]:
    by_row: dict[int, list[Cell]] = {}
    for cell in region.cells:
        by_row.setdefault(cell.row, []).append(cell)
    prints: list[RowPrint] = []
    for offset, sheet_row in enumerate(range(region.min_row, region.max_row + 1)):
        entry = RowPrint(index=offset, sheet_row=sheet_row)
        formula_cols: set[int] = set()
        for cell in by_row.get(sheet_row, []):
            col = cell.col - region.min_col
            rendered = _text(cell_value(cell)).strip()
            if not rendered:
                continue
            entry.values[col] = rendered
            entry.types[col] = _type_class(dtype_of([rendered]))
            entry.max_len = max(entry.max_len, len(rendered))
            if cell.formula:
                formula_cols.add(col)
                if _formula_spans_rows(cell.formula, cell.row):
                    entry.spanning_formula = True
        entry.formula_cols = frozenset(formula_cols)
        prints.append(entry)
    return prints


def _sentinels(prints: list[RowPrint], col: int) -> frozenset[str]:
    classes = [entry.types[col] for entry in prints if col in entry.types]
    if not classes or sum(1 for cls in classes if cls == "number") < len(classes) * _NUMERIC_SHARE:
        return frozenset()
    strings = Counter(entry.values[col] for entry in prints if entry.types.get(col) == "string")
    return frozenset(
        value for value, count in strings.items() if count >= _SENTINEL_MIN_REPEAT and len(value) <= _SENTINEL_MAX_LEN
    )


def _column_types(population: list[RowPrint], sentinels: dict[int, frozenset[str]]) -> dict[int, str]:
    counts: dict[int, Counter[str]] = {}
    for entry in population:
        for col, cls in entry.types.items():
            if entry.values.get(col) in sentinels.get(col, frozenset()):
                continue
            counts.setdefault(col, Counter())[cls] += 1
    return {col: counter.most_common(1)[0][0] for col, counter in counts.items()}


def _violations(entry: RowPrint, profile: Profile) -> list[int]:
    bad: list[int] = []
    for col, cls in entry.types.items():
        if col in profile.mixed or entry.values.get(col) in profile.sentinels.get(col, frozenset()):
            continue
        expected = profile.types.get(col)
        if expected is not None and expected != cls:
            bad.append(col)
    return bad


def _consistent(entry: RowPrint, profile: Profile) -> bool:
    if not entry.values:
        return False
    if frozenset(entry.values) & profile.rare:
        return False
    if _violations(entry, profile):
        return False
    return not (profile.median_arity >= 2 and len(entry.values) <= 1)


def _median_arity(population: list[RowPrint]) -> int:
    arities = sorted(len(entry.values) for entry in population)
    return arities[len(arities) // 2] if arities else 0


def _rare(prints: list[RowPrint]) -> frozenset[int]:
    counts: Counter[int] = Counter()
    for entry in prints:
        counts.update(entry.values.keys())
    return frozenset(col for col, count in counts.items() if count <= _RARE_MAX_ROWS)


def _core(population: list[RowPrint]) -> frozenset[int]:
    if not population:
        return frozenset()
    counts: Counter[int] = Counter()
    for entry in population:
        counts.update(entry.values.keys())
    threshold = len(population) * _CORE_SHARE
    return frozenset(col for col, count in counts.items() if count >= threshold)


def _mixed(prints: list[RowPrint], types: dict[int, str], sentinels: dict[int, frozenset[str]]) -> frozenset[int]:
    bad: Counter[int] = Counter()
    total: Counter[int] = Counter()
    for entry in prints:
        for col, cls in entry.types.items():
            if entry.values.get(col) in sentinels.get(col, frozenset()):
                continue
            total[col] += 1
            if types.get(col) != cls:
                bad[col] += 1
    return frozenset(col for col, count in bad.items() if count >= total[col] * _MIXED_SHARE)


def _settle(prints: list[RowPrint]) -> Profile:
    sentinels: dict[int, frozenset[str]] = {}
    seed = _column_types(prints, sentinels)
    for col in seed:
        found = _sentinels(prints, col)
        if found:
            sentinels[col] = found
    rare = _rare(prints)
    mixed = _mixed(prints, _column_types(prints, sentinels), sentinels)
    population = [entry for entry in prints if entry.values]
    profile = Profile(sentinels=sentinels, rare=rare, mixed=mixed)
    for _ in range(5):
        types = _column_types(population, sentinels)
        profile = Profile(
            types=types,
            core=_core(population),
            median_arity=_median_arity(population),
            mixed=mixed,
            rare=rare,
            sentinels=sentinels,
        )
        survivors = [entry for entry in prints if _consistent(entry, profile)]
        if not survivors or len(survivors) == len(population):
            return profile
        population = survivors
    return profile


def _column_formula_consistent(prints: list[RowPrint], entry: RowPrint) -> bool:
    if not entry.formula_cols:
        return False
    others = [other for other in prints if other.index != entry.index and other.values]
    if not others:
        return False
    return sum(1 for other in others if other.formula_cols & entry.formula_cols) >= len(others) / 2


def _segment(prints: list[RowPrint], profile: Profile) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    runs: list[tuple[int, int]] = []
    flagged: list[int] = []
    aggregates: list[int] = []
    current: list[int] = []
    for entry in prints:
        aggregate = entry.spanning_formula and not _column_formula_consistent(prints, entry)
        if aggregate:
            aggregates.append(entry.index)
        if _consistent(entry, profile) and not aggregate:
            current.append(entry.index)
            continue
        if current:
            runs.append((current[0], current[-1]))
            current = []
        flagged.append(entry.index)
    if current:
        runs.append((current[0], current[-1]))
    return runs, flagged, aggregates


def _extent_boundaries(prints: list[RowPrint]) -> list[tuple[int, int]]:
    spans: dict[int, tuple[int, int]] = {}
    for entry in prints:
        for col in entry.values:
            first, last = spans.get(col, (entry.index, entry.index))
            spans[col] = (min(first, entry.index), max(last, entry.index))
    edges: Counter[int] = Counter()
    for first, last in spans.values():
        if first > 0:
            edges[first] += 1
        if last < len(prints) - 1:
            edges[last + 1] += 1
    return sorted((row, count) for row, count in edges.items() if count >= _EXTENT_MIN_COLS)


def _restrict(prints: list[RowPrint], lo: int, hi: int) -> list[RowPrint]:
    return [
        RowPrint(
            index=entry.index,
            sheet_row=entry.sheet_row,
            values={col: value for col, value in entry.values.items() if lo <= col <= hi},
            types={col: cls for col, cls in entry.types.items() if lo <= col <= hi},
            max_len=entry.max_len,
            spanning_formula=entry.spanning_formula,
            formula_cols=frozenset(col for col in entry.formula_cols if lo <= col <= hi),
        )
        for entry in prints
    ]


def _key_overlap(prints: list[RowPrint], period: int, width: int) -> float:
    keys = [{entry.values[start] for entry in prints if start in entry.values} for start in range(0, width, period)]
    if len(keys) < 2 or not all(keys):
        return 0.0
    shared = set.intersection(*keys)
    largest = max(len(group) for group in keys)
    return len(shared) / largest if largest else 0.0


def _column_blocks(prints: list[RowPrint], profile: Profile) -> list[tuple[int, int]]:
    if not profile.types:
        return []
    width = max(profile.types) + 1
    for period in range(2, width // 2 + 1):
        if width % period:
            continue
        blocks = [
            tuple(profile.types.get(col) for col in range(start, start + period)) for start in range(0, width, period)
        ]
        if len(set(blocks)) != 1 or None in blocks[0]:
            continue
        if _key_overlap(prints, period, width) >= _KEY_OVERLAP_MIN:
            return [(start, start + period - 1) for start in range(0, width, period)]
    return []


def _label(sheet: SheetExtraction, index: int, region: Region) -> str:
    return f"sheet{sheet.sheet_no}_region{index}_r{region.min_row}-{region.max_row}_c{region.min_col}-{region.max_col}"


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/home/masterkenway/Downloads/ocr_input/test(1).xlsx"
    data = pathlib.Path(path).read_bytes()
    sheets = load_all_sheets(data)

    sum(len(sheet.cells) for sheet in sheets)
    sum(1 for sheet in sheets for cell in sheet.cells if cell.formula)
    sum(len(sheet.tables) for sheet in sheets)

    target = sys.argv[2] if len(sys.argv) > 2 else None

    for sheet in sheets:
        for index, region in enumerate(find_regions(sheet)):
            prints = _row_prints(region)
            profile = _settle(prints)
            runs, flagged, _aggregates = _segment(prints, profile)
            if target is not None and _label(sheet, index, region).startswith(target):
                for _entry in prints:
                    pass
            sum(end - start + 1 for start, end in runs) + len(flagged)
            max((end - start + 1 for start, end in runs), default=0)
            " SHOW_ALL" if len(prints) < _MIN_PROFILE_ROWS else ""
            _extent_boundaries(prints)
            for lo, hi in _column_blocks(prints, profile):
                block = _restrict(prints, lo, hi)
                block_profile = _settle(block)
                _block_runs, _block_flagged, _ = _segment(block, block_profile)


main()
