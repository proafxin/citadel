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


def stratified_sample(body: list[int], top: int, bottom: int, interval: int) -> list[int]:
    # of the rows the flagger calls body, take the top, the bottom, and every Nth in between. the interval stratum is
    # the recall backstop: if the flagger missed an internal header, an interval sample landing near it still lets the
    # model notice a boundary the flagger did not flag
    if not body:
        return []
    chosen = set(body[:top]) | set(body[-bottom:]) | set(body[::interval] if interval > 0 else [])
    return sorted(chosen)


SAMPLE_TOP = 4
SAMPLE_BOTTOM = 2
SAMPLE_INTERVAL = 25
PAYLOAD_LIMIT = 120  # the model never sees more than this many rows of a sheet, whatever its height


def payload_rows(grid: list[list[str]]) -> list[int]:
    # what the model actually sees: every interesting row (they are the boundaries), plus a stratified body sample,
    # capped at PAYLOAD_LIMIT. interesting rows are kept first — a boundary must not be dropped to fit the cap; the
    # body sample fills the rest. all index-tagged so the model reconstructs regions by position
    labels = classify_rows(grid)
    interesting = [index for index, label in enumerate(labels) if label == INTERESTING]
    body = [index for index, label in enumerate(labels) if label == BODY]
    sample = stratified_sample(body, SAMPLE_TOP, SAMPLE_BOTTOM, SAMPLE_INTERVAL)
    kept = interesting[:PAYLOAD_LIMIT]
    room = PAYLOAD_LIMIT - len(kept)
    if room > 0:
        kept = sorted(set(kept) | set(sample[:room]))
    return kept
