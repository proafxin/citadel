import re
from collections.abc import Callable
from dataclasses import dataclass, field

from citadel.tabular.signatures import (
    AGGREGATION_KEYWORDS,
    FOOTNOTE_KEYWORDS,
    CellSignature,
    LineSignature,
    cell_signature,
    common_symbol_prefix,
)

DATA_CONTEXT_WINDOW = 3

GUIDE_HEADERS = frozenset({"description", "field", "column", "variable", "name", "type", "value", "unit", "code"})
DATATYPE_TOKENS = frozenset({"integer", "int", "float", "decimal", "string", "text", "boolean", "date", "datetime"})

_INT_ONLY = re.compile(r"-?\d+")
_RANGE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:-|–|—|/|to)\s*\d+(?:[.,]\d+)?")


@dataclass
class LineContext:
    line: LineSignature
    below: list[LineSignature] = field(default_factory=list)
    first_col_only: bool = False


ColumnRule = Callable[[CellSignature, list[CellSignature]], bool]
LineRule = Callable[[LineContext], bool]


def _nonnull(cells: list[CellSignature]) -> list[CellSignature]:
    return [cell for cell in cells if not cell.null]


def _col(cell: CellSignature, ctx: list[CellSignature]) -> list[CellSignature]:
    return _nonnull([cell, *ctx])


def _prefix(cells: list[CellSignature], forward: bool) -> tuple[str, ...]:
    return common_symbol_prefix([c.symbols if forward else c.symbols_reversed for c in _nonnull(cells)])


def _prefix_len_no_space(prefix: tuple[str, ...]) -> int:
    return sum(1 for symbol in prefix if symbol != "W")


def _breaks(cell: CellSignature, context: list[CellSignature], forward: bool) -> bool:
    return _prefix(context, forward) != _prefix([*context, cell], forward)


def _common_runs(cells: list[CellSignature], forward: bool) -> tuple[tuple[str, int], ...]:
    sequences = [c.symbols if forward else c.symbols_reversed for c in _nonnull(cells)]
    if not sequences:
        return ()
    result: list[tuple[str, int]] = []
    for position in range(min(len(sequence) for sequence in sequences)):
        symbols = {sequence[position][0] for sequence in sequences}
        if len(symbols) != 1:
            break
        counts = {sequence[position][1] for sequence in sequences}
        result.append((next(iter(symbols)), min(counts)))
        if len(counts) != 1:
            break
    return tuple(result)


def _is_single_d_run(cells: list[CellSignature], forward: bool, minimum: int) -> bool:
    runs = _common_runs(cells, forward)
    return len(runs) == 1 and runs[0][0] == "D" and runs[0][1] >= minimum


def _first_two_no_space(cells: list[CellSignature], forward: bool) -> bool:
    prefix = _prefix(cells, forward)
    return len(prefix) >= 2 and prefix[0] != "W" and prefix[1] != "W"


def _is_d_star(cells: list[CellSignature]) -> bool:
    prefix = _prefix(cells, forward=True)
    return len(prefix) >= 2 and prefix[0] == "D" and "*" not in prefix[:2]


def _over_max(ratio: float) -> ColumnRule:
    return lambda cell, ctx: (
        bool(_nonnull(ctx)) and not cell.null and cell.length > ratio * max(c.length for c in _nonnull(ctx))
    )


COLUMN_DATA_RULES: dict[str, ColumnRule] = {
    "CONSISTENT_NUMERIC_WIDTH": lambda cell, ctx: (
        bool(ctx)
        and bool(_col(cell, ctx))
        and all(c.strict_numeric for c in _col(cell, ctx))
        and len({c.length for c in _col(cell, ctx)}) == 1
    ),
    "CONSISTENT_NUMERIC": lambda cell, ctx: (
        bool(ctx) and bool(_col(cell, ctx)) and all(c.strict_numeric for c in _col(cell, ctx))
    ),
    "CONSISTENT_D_STAR": lambda cell, ctx: bool(ctx) and _is_d_star([cell, *ctx]),
    "FW_SUMMARY_D": lambda cell, ctx: _prefix([cell, *ctx], forward=True)[:1] == ("D",),
    "BW_SUMMARY_D": lambda cell, ctx: _prefix([cell, *ctx], forward=False)[:1] == ("D",),
    "BROAD_NUMERIC": lambda cell, ctx: bool(ctx) and bool(_col(cell, ctx)) and all(c.numeric for c in _col(cell, ctx)),
    "FW_THREE_OR_MORE_NO_SPACE": lambda cell, ctx: _prefix_len_no_space(_prefix([cell, *ctx], forward=True)) >= 3,
    "BW_THREE_OR_MORE_NO_SPACE": lambda cell, ctx: _prefix_len_no_space(_prefix([cell, *ctx], forward=False)) >= 3,
    "CONSISTENT_SS_NO_SPACE": lambda cell, ctx: (
        bool(ctx)
        and bool(_col(cell, ctx))
        and len({c.symbol_set for c in _col(cell, ctx)}) == 1
        and "W" not in _col(cell, ctx)[0].symbol_set
    ),
    "CONSISTENT_SC_TWO_OR_MORE": lambda cell, ctx: (
        bool(ctx)
        and bool(_col(cell, ctx))
        and len({c.symbol_set for c in _col(cell, ctx)}) == 1
        and len(_col(cell, ctx)[0].symbol_set) >= 2
    ),
    "FW_TWO_OR_MORE_NO_SPACE": lambda cell, ctx: _prefix_len_no_space(_prefix([cell, *ctx], forward=True)) >= 2,
    "BW_TWO_OR_MORE_NO_SPACE": lambda cell, ctx: _prefix_len_no_space(_prefix([cell, *ctx], forward=False)) >= 2,
    "FW_TWO_OR_MORE_NO_SPACE_FIRST_TWO": lambda cell, ctx: _first_two_no_space([cell, *ctx], forward=True),
    "BW_TWO_OR_MORE_NO_SPACE_FIRST_TWO": lambda cell, ctx: _first_two_no_space([cell, *ctx], forward=False),
    "FW_D5PLUS": lambda cell, ctx: _is_single_d_run([cell, *ctx], forward=True, minimum=5),
    "BW_D5PLUS": lambda cell, ctx: _is_single_d_run([cell, *ctx], forward=False, minimum=5),
    "FW_D1": lambda cell, ctx: _common_runs([cell, *ctx], forward=True) == (("D", 1),),
    "BW_D1": lambda cell, ctx: _common_runs([cell, *ctx], forward=False) == (("D", 1),),
    "FW_D4": lambda cell, ctx: _common_runs([cell, *ctx], forward=True) == (("D", 4),),
    "BW_D4": lambda cell, ctx: _common_runs([cell, *ctx], forward=False) == (("D", 4),),
    "FW_LENGTH_4PLUS": lambda cell, ctx: len(_prefix([cell, *ctx], forward=True)) >= 4,
    "BW_LENGTH_4PLUS": lambda cell, ctx: len(_prefix([cell, *ctx], forward=False)) >= 4,
    "CASE_SUMMARY_CAPS": lambda cell, ctx: (
        bool(ctx) and bool(_col(cell, ctx)) and all(c.case == "upper" for c in _col(cell, ctx))
    ),
    "CONSISTENT_SINGLE_WORD_CONSISTENT_CASE": lambda cell, ctx: (
        bool(ctx)
        and bool(_col(cell, ctx))
        and len({c.case for c in _col(cell, ctx)}) == 1
        and _col(cell, ctx)[0].case in {"upper", "lower", "title"}
        and all("W" not in c.symbol_set for c in _col(cell, ctx))
    ),
    "VALUE_REPEATS_ONCE_BELOW": lambda cell, ctx: not cell.null and sum(1 for c in ctx if c.value == cell.value) == 1,
    "VALUE_REPEATS_TWICE_OR_MORE_BELOW": lambda cell, ctx: (
        not cell.null and sum(1 for c in ctx if c.value == cell.value) >= 2
    ),
    "CONSISTENT_CHAR_LENGTH": lambda cell, ctx: (
        bool(ctx) and bool(_col(cell, ctx)) and len({c.length for c in _col(cell, ctx)}) == 1
    ),
}

COLUMN_NOT_DATA_RULES: dict[str, ColumnRule] = {
    "First_FW_Symbol_disagrees": lambda cell, ctx: (
        bool(ctx)
        and bool(cell.symbols)
        and bool(_prefix(ctx, forward=True))
        and (cell.symbols[0][0],) != _prefix(ctx, forward=True)[:1]
    ),
    "First_BW_Symbol_disagrees": lambda cell, ctx: (
        bool(ctx)
        and bool(cell.symbols_reversed)
        and bool(_prefix(ctx, forward=False))
        and (cell.symbols_reversed[0][0],) != _prefix(ctx, forward=False)[:1]
    ),
    "SymbolChain": lambda cell, ctx: (
        len(_nonnull(ctx)) >= 2
        and len({c.symbols for c in _nonnull(ctx)}) == 1
        and not cell.null
        and cell.symbols != _nonnull(ctx)[0].symbols
    ),
    "CONSISTENT_NUMERIC": lambda cell, ctx: (
        bool(_nonnull(ctx))
        and all(c.strict_numeric for c in _nonnull(ctx))
        and not cell.null
        and not cell.strict_numeric
    ),
    "CONSISTENT_D_STAR": lambda cell, ctx: bool(ctx) and _is_d_star(ctx) and not _is_d_star([*ctx, cell]),
    "FW_SUMMARY_D": lambda cell, ctx: _prefix(ctx, forward=True)[:1] == ("D",) and _breaks(cell, ctx, forward=True),
    "BW_SUMMARY_D": lambda cell, ctx: _prefix(ctx, forward=False)[:1] == ("D",) and _breaks(cell, ctx, forward=False),
    "BROAD_NUMERIC": lambda cell, ctx: (
        bool(_nonnull(ctx)) and all(c.numeric for c in _nonnull(ctx)) and not cell.null and not cell.numeric
    ),
    "FW_THREE_OR_MORE_NO_SPACE": lambda cell, ctx: (
        _prefix_len_no_space(_prefix(ctx, forward=True)) >= 3 and _breaks(cell, ctx, forward=True)
    ),
    "BW_THREE_OR_MORE_NO_SPACE": lambda cell, ctx: (
        _prefix_len_no_space(_prefix(ctx, forward=False)) >= 3 and _breaks(cell, ctx, forward=False)
    ),
    "CONSISTENT_SS_NO_SPACE": lambda cell, ctx: (
        bool(_nonnull(ctx))
        and len({c.symbol_set for c in _nonnull(ctx)}) == 1
        and "W" not in _nonnull(ctx)[0].symbol_set
        and not cell.null
        and cell.symbol_set != _nonnull(ctx)[0].symbol_set
    ),
    "FW_TWO_OR_MORE_NO_SPACE": lambda cell, ctx: (
        _prefix_len_no_space(_prefix(ctx, forward=True)) >= 2 and _breaks(cell, ctx, forward=True)
    ),
    "BW_TWO_OR_MORE_NO_SPACE": lambda cell, ctx: (
        _prefix_len_no_space(_prefix(ctx, forward=False)) >= 2 and _breaks(cell, ctx, forward=False)
    ),
    "CC": lambda cell, ctx: (
        bool(_nonnull(ctx))
        and len({c.case for c in _nonnull(ctx)}) == 1
        and not cell.null
        and cell.case != _nonnull(ctx)[0].case
    ),
    "CHAR_COUNT_UNDER_POINT1_MIN": lambda cell, ctx: (
        bool(_nonnull(ctx)) and not cell.null and cell.length < 0.1 * min(c.length for c in _nonnull(ctx))
    ),
    "CHAR_COUNT_UNDER_POINT3_MIN": lambda cell, ctx: (
        bool(_nonnull(ctx)) and not cell.null and cell.length < 0.3 * min(c.length for c in _nonnull(ctx))
    ),
    "CHAR_COUNT_OVER_POINT5_MAX": _over_max(1.5),
    "CHAR_COUNT_OVER_POINT6_MAX": _over_max(1.6),
    "CHAR_COUNT_OVER_POINT7_MAX": _over_max(1.7),
    "CHAR_COUNT_OVER_POINT8_MAX": _over_max(1.8),
    "CHAR_COUNT_OVER_POINT9_MAX": _over_max(1.9),
    "NON_NUMERIC_CHAR_COUNT_DIFFERS_FROM_CONSISTENT": lambda cell, ctx: (
        bool(_nonnull(ctx))
        and len({c.length for c in _nonnull(ctx)}) == 1
        and not cell.null
        and cell.length != _nonnull(ctx)[0].length
        and not any(c.numeric for c in [cell, *_nonnull(ctx)])
    ),
}


def _cell_numbers(line: LineSignature) -> list[int | None]:
    numbers: list[int | None] = []
    for cell in line.cells:
        text = cell.value.replace(" ", "").replace(",", "")
        numbers.append(int(text) if _INT_ONLY.fullmatch(text) else None)
    return numbers


def _has_arith_window(segment: list[int], length: int) -> bool:
    for start in range(len(segment) - length + 1):
        window = segment[start : start + length]
        step = window[1] - window[0]
        if step != 0 and all(window[i + 1] - window[i] == step for i in range(length - 1)):
            return True
    return False


def _arithmetic_run(line: LineSignature, length: int) -> bool:
    numbers = _cell_numbers(line)
    index = 0
    while index < len(numbers):
        if numbers[index] is None:
            index += 1
            continue
        end = index + 1
        while end < len(numbers) and numbers[end] is not None:
            end += 1
        segment = [value for value in numbers[index:end] if value is not None]
        if len(segment) >= length and _has_arith_window(segment, length):
            return True
        index = end
    return False


def _range_count(line: LineSignature) -> int:
    return sum(1 for cell in line.cells if not cell.null and _RANGE.fullmatch(cell.value))


def _partially_repeating(line: LineSignature) -> bool:
    positions: dict[str, list[int]] = {}
    for index, cell in enumerate(line.cells):
        if cell.null or cell.numeric or "A" not in cell.symbol_set:
            continue
        positions.setdefault(cell.value, []).append(index)
    return any(
        len(indices) >= 2 and len({indices[k + 1] - indices[k] for k in range(len(indices) - 1)}) == 1
        for indices in positions.values()
    )


def _no_summary_below(lc: LineContext) -> bool:
    if not lc.below:
        return False
    width = max(len(line.cells) for line in lc.below)
    for column in range(width):
        cells = [line.cells[column] for line in lc.below if column < len(line.cells)]
        prefix = _prefix(cells, forward=True)
        if prefix and prefix[0] != "*":
            return False
    return True


def _is_camel(value: str) -> bool:
    return (
        bool(value) and " " not in value and value[:1].islower() and any(c.isupper() for c in value) and value.isalnum()
    )


def _aggregation_after_first(line: LineSignature) -> bool:
    return any(keyword in cell.value.casefold() for cell in line.cells[1:] for keyword in AGGREGATION_KEYWORDS)


LINE_DATA_RULES: dict[str, LineRule] = {
    "AGGREGATION_TOKEN_IN_FIRST_VALUE_OF_ROW": lambda lc: (
        bool(lc.line.cells) and any(keyword in lc.line.cells[0].value.casefold() for keyword in AGGREGATION_KEYWORDS)
    ),
    "NULL_EQUIVALENT_ON_LINE_2_PLUS": lambda lc: sum(1 for cell in lc.line.cells if cell.null) >= 2,
    "ONE_NULL_EQUIVALENT_ON_LINE": lambda lc: sum(1 for cell in lc.line.cells if cell.null) == 1,
    "CONTAINS_DATATYPE_CELL_VALUE": lambda lc: any(cell.value.casefold() in DATATYPE_TOKENS for cell in lc.line.cells),
}

LINE_NOT_DATA_RULES: dict[str, LineRule] = {
    "METADATA_LIKE_ROW": lambda lc: (
        bool(lc.line.cells)
        and lc.line.cells[0].value.startswith("(")
        and lc.line.cells[0].value.endswith(")")
        and all(cell.null for cell in lc.line.cells[1:])
    ),
    "UP_TO_FIRST_COLUMN_COMPLETE_CONSISTENTLY": lambda lc: lc.first_col_only,
    "STARTS_WITH_NULL": lambda lc: bool(lc.line.cells) and lc.line.cells[0].null,
    "NO_SUMMARY_BELOW": _no_summary_below,
    "CONSISTENTLY_SLUG_OR_SNAKE": lambda lc: (
        bool(lc.line.non_null) and all(cell.case == "snake" or _is_camel(cell.value) for cell in lc.line.non_null)
    ),
    "CONSISTENTLY_UPPER_CASE": lambda lc: (
        bool(lc.line.non_null) and all(cell.case == "upper" for cell in lc.line.non_null)
    ),
    "ADJACENT_ARITHMETIC_SEQUENCE_2": lambda lc: _arithmetic_run(lc.line, 2),
    "ADJACENT_ARITHMETIC_SEQUENCE_3": lambda lc: _arithmetic_run(lc.line, 3),
    "ADJACENT_ARITHMETIC_SEQUENCE_4": lambda lc: _arithmetic_run(lc.line, 4),
    "ADJACENT_ARITHMETIC_SEQUENCE_5": lambda lc: _arithmetic_run(lc.line, 5),
    "ADJACENT_ARITHMETIC_SEQUENCE_6_plus": lambda lc: _arithmetic_run(lc.line, 6),
    "RANGE_PAIRS_1": lambda lc: _range_count(lc.line) == 1,
    "RANGE_PAIRS_2_plus": lambda lc: _range_count(lc.line) >= 2,
    "PARTIALLY_REPEATING_VALUES_length_2_plus": lambda lc: _partially_repeating(lc.line),
    "AGGREGATION_ON_ROW_WO_NUMERIC": lambda lc: (
        _aggregation_after_first(lc.line) and not any(cell.numeric for cell in lc.line.cells)
    ),
    "AGGREGATION_ON_ROW_W_ARITH_SEQUENCE": lambda lc: _aggregation_after_first(lc.line) and _arithmetic_run(lc.line, 2),
    "FOOTNOTE": lambda lc: (
        bool(lc.line.cells)
        and any(lc.line.cells[0].value.casefold().startswith(keyword) for keyword in FOOTNOTE_KEYWORDS)
        and all(cell.null for cell in lc.line.cells[2:])
    ),
    "METADATA_TABLE_HEADER_KEYWORDS": lambda lc: any(cell.value.casefold() in GUIDE_HEADERS for cell in lc.line.cells),
}

COLUMN_FEATURE_NAMES = tuple(f"CD:{name}" for name in COLUMN_DATA_RULES) + tuple(
    f"CN:{name}" for name in COLUMN_NOT_DATA_RULES
)
LINE_FEATURE_NAMES = tuple(f"LD:{name}" for name in LINE_DATA_RULES) + tuple(
    f"LN:{name}" for name in LINE_NOT_DATA_RULES
)
FEATURE_NAMES = COLUMN_FEATURE_NAMES + LINE_FEATURE_NAMES


def _signature_grid(rows: list[list[str]]) -> tuple[list[list[CellSignature]], int]:
    width = max((len(row) for row in rows), default=0)
    grid = [[cell_signature(value) for value in [*row, *[""] * (width - len(row))]] for row in rows]
    return grid, width


def _column_context(grid: list[list[CellSignature]], row_index: int, column: int, n: int) -> list[CellSignature]:
    end = min(n, row_index + 1 + DATA_CONTEXT_WINDOW)
    return [grid[k][column] for k in range(row_index + 1, end)]


def _column_features(grid: list[list[CellSignature]], row_index: int, width: int, n: int) -> list[float]:
    pairs = [(grid[row_index][column], _column_context(grid, row_index, column, n)) for column in range(width)]
    data = [sum(1 for cell, ctx in pairs if rule(cell, ctx)) / width for rule in COLUMN_DATA_RULES.values()]
    not_data = [sum(1 for cell, ctx in pairs if rule(cell, ctx)) / width for rule in COLUMN_NOT_DATA_RULES.values()]
    return [*data, *not_data]


def _line_features(lc: LineContext) -> list[float]:
    data = [1.0 if rule(lc) else 0.0 for rule in LINE_DATA_RULES.values()]
    not_data = [1.0 if rule(lc) else 0.0 for rule in LINE_NOT_DATA_RULES.values()]
    return [*data, *not_data]


def featurize(rows: list[list[str]]) -> list[list[float]]:
    grid, width = _signature_grid(rows)
    n = len(grid)
    if n == 0:
        return []
    lines = [LineSignature(cells=cells) for cells in grid]
    vectors: list[list[float]] = []
    first_col_only = True
    for row_index in range(n):
        first_col_only = first_col_only and all(cell.null for cell in lines[row_index].cells[1:])
        lc = LineContext(
            line=lines[row_index],
            below=lines[row_index + 1 : row_index + 1 + DATA_CONTEXT_WINDOW],
            first_col_only=first_col_only,
        )
        column = _column_features(grid, row_index, width, n) if width else [0.0] * len(COLUMN_FEATURE_NAMES)
        vectors.append([*column, *_line_features(lc)])
    return vectors
