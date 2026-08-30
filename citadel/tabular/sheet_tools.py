import re
from collections import Counter

from openpyxl.utils import column_index_from_string, get_column_letter
from pydantic import BaseModel, Field

from citadel.tabular.sheet_dump import Cell, SheetDump

RANGE = re.compile(r"^\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?$")
AGGREGATE = re.compile(r"\b(SUM|AVERAGE|AVERAGEA|COUNT|COUNTA|SUBTOTAL|MIN|MAX)\(([^()]*?:[^()]*?)\)")
MAX_HITS = 200
MAX_VALUES = 50


class FindHit(BaseModel):
    row: int
    text: str


class FindResult(BaseModel):
    pattern: str
    count: int
    truncated: bool
    rows: list[FindHit] = Field(default_factory=list)


class ValueCount(BaseModel):
    value: str
    count: int


class ColumnValues(BaseModel):
    column: str
    populated: int
    distinct: int
    truncated: bool
    values: list[ValueCount] = Field(default_factory=list)


class RangeResult(BaseModel):
    ref: str
    rows: int
    columns: int
    populated: int
    cells: list[Cell] = Field(default_factory=list)


class ToolError(Exception):
    pass


def bounds(dump: SheetDump, from_row: int | None, to_row: int | None) -> tuple[int, int]:
    start = dump.first_row if from_row is None else max(from_row, dump.first_row)
    end = dump.last_row if to_row is None else min(to_row, dump.last_row)
    return start, end


def find(dump: SheetDump, pattern: str, from_row: int | None = None, to_row: int | None = None) -> FindResult:
    try:
        matcher = re.compile(pattern)
    except re.error as exc:
        message = f"invalid pattern: {exc}"
        raise ToolError(message) from exc
    start, end = bounds(dump, from_row, to_row)
    hits = [
        FindHit(row=row, text=dump.lines[row])
        for row in range(start, end + 1)
        if row in dump.lines and matcher.search(dump.lines[row])
    ]
    return FindResult(
        pattern=pattern, count=len(hits), truncated=len(hits) > MAX_HITS, rows=hits[:MAX_HITS]
    )


def column_values(
    dump: SheetDump,
    column: str,
    from_row: int | None = None,
    to_row: int | None = None,
    limit: int = MAX_VALUES,
) -> ColumnValues:
    letters = column.strip().upper()
    if not letters.isalpha():
        message = f"invalid column: {column}"
        raise ToolError(message)
    index = column_index_from_string(letters)
    start, end = bounds(dump, from_row, to_row)
    counter: Counter[str] = Counter()
    for row in range(start, end + 1):
        cell = dump.cell(row, index)
        if cell is not None and cell.value:
            counter[cell.value] += 1
    ordered = counter.most_common()
    return ColumnValues(
        column=letters,
        populated=sum(counter.values()),
        distinct=len(counter),
        truncated=len(ordered) > limit,
        values=[ValueCount(value=value, count=count) for value, count in ordered[:limit]],
    )


def read_range(dump: SheetDump, ref: str) -> RangeResult:
    match = RANGE.match(ref.strip().upper().replace(" ", ""))
    if not match:
        message = f"invalid range: {ref}"
        raise ToolError(message)
    col_a = column_index_from_string(match.group(1))
    row_a = int(match.group(2))
    col_b = column_index_from_string(match.group(3)) if match.group(3) else col_a
    row_b = int(match.group(4)) if match.group(4) else row_a
    row_start, row_end = min(row_a, row_b), max(row_a, row_b)
    col_start, col_end = min(col_a, col_b), max(col_a, col_b)
    cells = [
        cell
        for row in range(row_start, row_end + 1)
        for column in range(col_start, col_end + 1)
        if (cell := dump.cell(row, column)) is not None
    ]
    normalised = (
        f"{get_column_letter(col_start)}{row_start}:{get_column_letter(col_end)}{row_end}"
    )
    return RangeResult(
        ref=normalised,
        rows=row_end - row_start + 1,
        columns=col_end - col_start + 1,
        populated=len(cells),
        cells=cells,
    )
