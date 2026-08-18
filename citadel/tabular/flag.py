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
    width = max((len(row) for row in grid), default=0)
    body = grid[1:] or grid
    kinds: list[str] = []
    for col in range(width):
        counts = Counter(k for row in body if col < len(row) and (k := _kind(row[col])) != "empty")
        kinds.append(counts.most_common(1)[0][0] if counts else "text")
    return kinds


SAMPLE_TOP = 4
SAMPLE_BOTTOM = 2
SAMPLE_INTERVAL = 25
PAYLOAD_LIMIT = 20


def _strided(rows: list[int], room: int) -> list[int]:
    if room <= 0 or not rows:
        return []
    stride = max(SAMPLE_INTERVAL, -(-len(rows) // room))
    return rows[::stride][:room]


def stratified_sample(rows: list[int], top: int, bottom: int, room: int) -> list[int]:
    if not rows:
        return []
    edges = set(rows[:top]) | set(rows[len(rows) - bottom :])
    middle = [index for index in rows[top : len(rows) - bottom] if index not in edges]
    return sorted(edges | set(_strided(middle, room - len(edges))))


def payload_rows(grid: list[list[str]]) -> list[int]:
    return stratified_sample(list(range(len(grid))), SAMPLE_TOP, SAMPLE_BOTTOM, PAYLOAD_LIMIT)
