import asyncio
import pathlib
import sys

from scan_simulate import RowPrint, _anchor, _row_prints

from citadel.llm import call_structured, call_text, count_tokens
from citadel.services.excel import find_regions, load_all_sheets

WINDOW_BUDGET = 8192
REPEATS = 3

PROMPT = """# Table Detection

You are given lines extracted from a region of an excel sheet cell grid. The region may continue past the last \
line shown. Consider the lines together, and check contextually whether one or more meaningful tables begin \
here. Report how many begin here, and for each one, where it starts (start row, start column, end column) and \
the first row that holds a data record.
"""

EXTRACT_PROMPT = """# Table Detection Extraction

You are given an answer describing how many tables a region contains, where each one starts (start row, start \
column, end column) and the first row of each one that holds a data record. Extract that into structured form, \
one entry per table.
"""

ANCHOR_SCHEMA = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_row": {"type": "integer"},
                    "col_start": {"type": "integer"},
                    "col_end": {"type": "integer"},
                    "first_data_row": {"type": "integer"},
                },
                "required": ["start_row", "col_start", "col_end", "first_data_row"],
            },
        }
    },
    "required": ["tables"],
}


def _render(entry: RowPrint, extent: frozenset[int]) -> str:
    width = max(extent) + 1 if extent else 0
    cells = [f"col{col}: {entry.values.get(col, '')}" for col in range(width)]
    return f"row {entry.index}: " + " | ".join(cells)


def _window(prints: list[RowPrint], extent: frozenset[int], budget: int) -> list[str]:
    lines: list[str] = []
    used = 0
    for entry in prints:
        line = _render(entry, extent)
        cost = count_tokens(line)
        if used + cost > budget and lines:
            break
        lines.append(line)
        used += cost
    return lines


async def _probe(label: str, text: str, max_tokens: int, attempt: int) -> tuple[str, int, int, list]:
    raw = await call_text(f"{PROMPT}\n\n{text}", max_tokens=max_tokens, key=f"anchor:{label}:{attempt}")
    data = await call_structured(f"{EXTRACT_PROMPT}\n\n{raw}", ANCHOR_SCHEMA)
    entries = [
        (item.get("start_row"), item.get("col_start"), item.get("col_end"), item.get("first_data_row"))
        for item in data.get("tables") or []
    ]
    return label, attempt, count_tokens(raw), entries


async def main() -> None:
    path = "/home/masterkenway/Downloads/ocr_input/test(1).xlsx"
    max_tokens = int(sys.argv[1]) if len(sys.argv) > 1 else 768
    target = sys.argv[2] if len(sys.argv) > 2 else None

    blob = pathlib.Path(path).read_bytes()

    windows: list[tuple[str, int, int, str]] = []
    for sheet in load_all_sheets(blob):
        for index, region in enumerate(find_regions(sheet)):
            label = f"sheet{sheet.sheet_no}_region{index}"
            if target is not None and label != target:
                continue
            prints = _row_prints(region)
            _types, extent = _anchor(prints)
            lines = _window(prints, extent, WINDOW_BUDGET)
            windows.append((label, len(prints), len(lines), "\n".join(lines)))

    results = await asyncio.gather(
        *(
            _probe(label, text, max_tokens, attempt)
            for label, _rows, _shown, text in windows
            for attempt in range(REPEATS)
        )
    )
    grouped: dict[str, list[tuple[int, int, list]]] = {}
    for label, attempt, tokens, entries in results:
        grouped.setdefault(label, []).append((attempt, tokens, entries))

    for label, _rows, _shown, _text in windows:
        for attempt, tokens, entries in sorted(grouped.get(label, [])):
            "TRUNC" if tokens >= max_tokens - 8 else "     "


if __name__ == "__main__":
    asyncio.run(main())
