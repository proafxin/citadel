import re
from collections import Counter

_NUMBER = re.compile(r"^[-+]?\d[\d,]*\.?\d*([eE][-+]?\d+)?$")
_DATE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}")

INTERESTING = "interesting"
BODY = "body"

# the deterministic front of the tabular stage. it does NOT decide headers or regions — it flags which rows are
# INTERESTING (a possible header / boundary / anomaly) so only those, plus a bounded body sample, go to the model.
# the one rule it must honour is RECALL: a real header must never be classified as body, because a row the flagger
# calls body is only ever SAMPLED, and a missed header sampled away is a header the model cannot see. so the test is
# subtractive — a row is interesting UNLESS it is provably body — and every uncertain call resolves to interesting.

MIN_SPARSE_FLOOR = 2  # a row this sparse is a banner/section marker, not a data row, however wide the table


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


def _populated(row: list[str]) -> int:
    return sum(1 for cell in row if cell.strip())


def classify_rows(grid: list[list[str]]) -> list[str]:
    # per-row INTERESTING / BODY. a row is BODY only when it is provably data: its typed columns hold the type the body
    # holds, and it is not a shape anomaly. everything else — a type break, a blank, a sparse banner, or ANY row when
    # the table has no typed column to anchor against — is interesting
    kinds = column_kinds(grid)
    typed = [index for index, kind in enumerate(kinds) if kind in {"number", "date"}]
    populated = [_populated(row) for row in grid]
    modal = Counter(count for count in populated if count).most_common(1)
    modal_pop = modal[0][0] if modal else 0
    labels: list[str] = []
    for index, row in enumerate(grid):
        if populated[index] == 0:
            labels.append(INTERESTING)  # a blank row is a boundary, never body
            continue
        if not typed:
            labels.append(INTERESTING)  # all-string table: no type anchor, so every row is a header candidate
            continue
        breaks_type = any(col < len(row) and _kind(row[col]) == "text" for col in typed)
        sparse = populated[index] < max(MIN_SPARSE_FLOOR, modal_pop // 2)
        labels.append(INTERESTING if breaks_type or sparse else BODY)
    return labels


def interesting_rows(grid: list[list[str]]) -> list[int]:
    return [index for index, label in enumerate(classify_rows(grid)) if label == INTERESTING]


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
    # what the model actually sees, bounded at PAYLOAD_LIMIT rows however tall the sheet. the SLM reads the SCHEMA and
    # writes a description; neither needs every row, so the stratified sample is taken FIRST and always survives, and
    # the flagged boundary rows fill whatever room is left. all index-tagged so the model places regions by position
    kept = set(stratified_sample(list(range(len(grid))), SAMPLE_TOP, SAMPLE_BOTTOM, PAYLOAD_LIMIT))
    for index, label in enumerate(classify_rows(grid)):
        if len(kept) >= PAYLOAD_LIMIT:
            break
        if label == INTERESTING:
            kept.add(index)
    return sorted(kept)
