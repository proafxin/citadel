import re
from collections import Counter

_NUMBER = re.compile(r"^[-+]?\d[\d,]*\.?\d*([eE][-+]?\d+)?$")
_DATE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}")


def _kind(cell: str) -> str:
    value = cell.strip()
    if not value:
        return "empty"
    if _NUMBER.match(value):
        return "number"
    if _DATE.match(value):
        return "date"
    return "text"


def column_kinds(grid: list[list[str]]) -> list[str]:
    # each column's dominant non-empty kind, taken over the whole column. this is the body's type profile, and it is
    # what a header BREAKS: a column that is numbers below is text ("FY '09") in its header
    width = max((len(row) for row in grid), default=0)
    kinds: list[str] = []
    for col in range(width):
        counts = Counter(k for row in grid if col < len(row) and (k := _kind(row[col])) != "empty")
        kinds.append(counts.most_common(1)[0][0] if counts else "text")
    return kinds


SAMPLE_TOP = 4
SAMPLE_BOTTOM = 2
SAMPLE_INTERVAL = 25
PAYLOAD_LIMIT = 20  # the model never sees more than this many rows of a sheet, whatever its height. the
# sample exists to SHOW what the data looks like, not to carry it: the schema is in the column headers (every
# one of which is sent, at any width) and a handful of rows is enough to type them and describe the table


def _strided(rows: list[int], room: int) -> list[int]:
    # every Mth row, with M widened when the sheet is tall enough that every 25th would exceed the room it is given.
    # so the interval stratum spans the WHOLE middle at any height instead of covering a prefix and being cut off
    if room <= 0 or not rows:
        return []
    stride = max(SAMPLE_INTERVAL, -(-len(rows) // room))
    return rows[::stride][:room]


def stratified_sample(rows: list[int], top: int, bottom: int, room: int) -> list[int]:
    # top K, bottom K, and every Mth in between, bounded above by `room` at any table height. the interval stratum is
    # the recall backstop: if an internal header sits mid-table, a sample landing near it still lets the model notice
    # a boundary. the edges are taken first — the header is at the top and the totals are at the bottom
    if not rows:
        return []
    edges = set(rows[:top]) | set(rows[len(rows) - bottom :])
    middle = [index for index in rows[top : len(rows) - bottom] if index not in edges]
    return sorted(edges | set(_strided(middle, room - len(edges))))


def payload_rows(grid: list[list[str]]) -> list[int]:
    # what the model actually sees, bounded at PAYLOAD_LIMIT rows however tall the sheet: top K, bottom K, every Mth
    # in between. there is no row classification on our side — the model identifies the regions; these rows only show
    # it what the data looks like. all index-tagged so it places what it finds by position
    return stratified_sample(list(range(len(grid))), SAMPLE_TOP, SAMPLE_BOTTOM, PAYLOAD_LIMIT)
