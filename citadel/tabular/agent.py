import logging
from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, Field

from citadel.llm import call_structured, count_tokens, count_tokens_batch
from citadel.prompts import load_prompt
from citadel.services.capacity import get_text_capacity
from citadel.services.excel import Cell, SheetExtraction, cell_value

logger = logging.getLogger(__name__)

OVERVIEW_BUDGET = 49152
MAX_STEPS = 24
MAX_IDLE_STEPS = 2
MAX_LISTED_ROWS = 200
READ_BUDGET = 16384


class TableSpec(BaseModel):
    row_span: tuple[int, int]
    col_span: tuple[int, int]
    header_rows: list[int] = Field(default_factory=list)
    header_cols: list[int] = Field(default_factory=list)
    label_rows: list[int] = Field(default_factory=list)
    title_row: int | None = None
    note_rows: list[int] = Field(default_factory=list)


class StepLog(BaseModel):
    step: int
    prompt: str
    action: dict


class SheetResult(BaseModel):
    log: list[StepLog] = Field(default_factory=list)
    tables: list[TableSpec]
    dismissed: list[tuple[int, int, int, int]]
    unclaimed: int
    steps: int
    problems: list[str]


class AgentState(BaseModel):
    tables: list[TableSpec] = Field(default_factory=list)
    dismissed: list[tuple[int, int, int, int]] = Field(default_factory=list)
    transcript: list[str] = Field(default_factory=list)
    steps: int = 0
    idle: int = 0
    stopped: str | None = None


_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["emit", "dismiss", "amend", "read", "rows_where", "outline", "done"]},
        "range": {"type": ["array", "null"], "items": {"type": "integer"}, "minItems": 4, "maxItems": 4},
        "column": {"type": ["integer", "null"]},
        "holds": {"type": ["string", "null"], "enum": ["nothing", "text", "number", "date", "formula", None]},
        "index": {"type": ["integer", "null"]},
        "table": {
            "type": ["object", "null"],
            "properties": {
                "row_span": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
                "col_span": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
                "header_rows": {"type": "array", "items": {"type": "integer"}},
                "header_cols": {"type": "array", "items": {"type": "integer"}},
                "label_rows": {"type": "array", "items": {"type": "integer"}},
                "title_row": {"type": ["integer", "null"]},
                "note_rows": {"type": "array", "items": {"type": "integer"}},
            },
            "required": [
                "row_span",
                "col_span",
                "header_rows",
                "header_cols",
                "label_rows",
                "title_row",
                "note_rows",
            ],
        },
    },
    "required": ["action"],
}


def _render_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _by_row(cells: Iterable[Cell]) -> dict[int, list[Cell]]:
    rows: dict[int, list[Cell]] = {}
    for cell in cells:
        rows.setdefault(cell.row, []).append(cell)
    return rows


def _cell_text(cell: Cell) -> str:
    value = cell_value(cell)
    body = "" if value is None else _render_value(value).strip()
    return f"c{cell.col}={body}" + ("*" if cell.bold else "")


def render_range(sheet: SheetExtraction, box: tuple[int, int, int, int], budget: int | None = None) -> str:
    top, left, bottom, right = box
    inside = [c for c in sheet.cells if top <= c.row <= bottom and left <= c.col <= right]
    if not inside:
        return "no populated cells in this range"
    rows = _by_row(inside)
    lines = []
    for row in sorted(rows):
        ordered = sorted(rows[row], key=lambda cell: cell.col)
        lines.append(f"r{row}: " + " | ".join(_cell_text(cell) for cell in ordered))
    if budget is None:
        return "\n".join(lines)
    kept: list[str] = []
    used = 0
    for line, cost in zip(lines, count_tokens_batch(lines), strict=True):
        if kept and used + cost > budget:
            break
        kept.append(line)
        used += cost
    if len(kept) == len(lines):
        return "\n".join(lines)
    shown = kept[-1].split(":", 1)[0]
    note = f"[showed the first {len(kept)} of {len(lines)} populated rows, up to {shown}; read a smaller range]"
    return "\n".join([*kept, note])


def outline(sheet: SheetExtraction) -> str:
    rows = sorted({cell.row for cell in sheet.cells})
    by_row = _by_row(sheet.cells)
    blocks: list[tuple[int, int]] = []
    start = previous = rows[0]
    for row in rows[1:]:
        if row > previous + 1:
            blocks.append((start, previous))
            start = row
        previous = row
    blocks.append((start, previous))
    lines = [f"{len(blocks)} blocks of consecutive populated rows"]
    for top, bottom in blocks:
        cols = [cell.col for row in range(top, bottom + 1) for cell in by_row.get(row, [])]
        first = sorted(by_row[top], key=lambda cell: cell.col)
        head = " | ".join(_cell_text(cell) for cell in first)
        lines.append(f"r{top}-{bottom} c{min(cols)}-{max(cols)} ({bottom - top + 1} rows): {head[:110]}")
    return "\n".join(lines)


def _holds(cell: Cell | None, holds: str) -> bool:
    if holds == "nothing":
        return cell is None or cell_value(cell) is None or not str(cell_value(cell)).strip()
    if cell is None:
        return False
    if holds == "formula":
        return cell.formula is not None
    value = cell.value
    if holds == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if holds == "date":
        return isinstance(value, datetime)
    return isinstance(value, str) and bool(value.strip())


def rows_where(sheet: SheetExtraction, column: int, holds: str) -> str:
    located = {cell.row: cell for cell in sheet.cells if cell.col == column}
    rows = sorted({cell.row for cell in sheet.cells})
    hits = [row for row in rows if _holds(located.get(row), holds)]
    if not hits:
        return f"no populated row has {holds} in column {column}"
    shown = ", ".join(str(row) for row in hits[:MAX_LISTED_ROWS])
    more = f" (+{len(hits) - MAX_LISTED_ROWS} more)" if len(hits) > MAX_LISTED_ROWS else ""
    return f"{len(hits)} of {len(rows)} populated rows have {holds} in column {column}: {shown}{more}"


def _extent(sheet: SheetExtraction) -> tuple[int, int, int, int]:
    rows = [cell.row for cell in sheet.cells]
    cols = [cell.col for cell in sheet.cells]
    return min(rows), min(cols), max(rows), max(cols)


def sheet_facts(sheet: SheetExtraction) -> str:
    top, left, bottom, right = _extent(sheet)
    lines = [
        f"sheet {sheet.sheet_no} {sheet.sheet_name!r}",
        f"populated rows {top}-{bottom}, columns {left}-{right}, {len(sheet.cells)} non-empty cells",
    ]
    if sheet.merges:
        merged = ", ".join(f"r{a}c{b}:r{c}c{d}" for a, b, c, d in sheet.merges[:60])
        lines.append(f"merged ranges: {merged}")
    return "\n".join(lines)


def _covered(spec: TableSpec) -> set[tuple[int, int]]:
    top, bottom = spec.row_span
    left, right = spec.col_span
    return {(row, col) for row in range(top, bottom + 1) for col in range(left, right + 1)}


def _box_cells(box: tuple[int, int, int, int]) -> set[tuple[int, int]]:
    top, left, bottom, right = box
    return {(row, col) for row in range(top, bottom + 1) for col in range(left, right + 1)}


def unclaimed(
    sheet: SheetExtraction, tables: list[TableSpec], dismissed: list[tuple[int, int, int, int]]
) -> set[tuple[int, int]]:
    populated = {(cell.row, cell.col) for cell in sheet.cells}
    for spec in tables:
        populated -= _covered(spec)
    for box in dismissed:
        populated -= _box_cells(box)
    return populated


def _summarize(points: set[tuple[int, int]]) -> str:
    rows = sorted({row for row, _ in points})
    if not rows:
        return "none"
    return f"{len(points)} cells across rows {rows[0]}-{rows[-1]} ({len(rows)} rows)"


def verify(sheet: SheetExtraction, tables: list[TableSpec]) -> list[str]:
    populated = {(cell.row, cell.col) for cell in sheet.cells}
    problems: list[str] = []
    seen: set[tuple[int, int]] = set()
    for index, spec in enumerate(tables):
        box = _covered(spec)
        if not box & populated:
            problems.append(f"table {index} covers no populated cell")
        overlap = box & seen
        if overlap:
            problems.append(f"table {index} overlaps an earlier table at {len(overlap)} cells")
        seen |= box
        top, bottom = spec.row_span
        listed = spec.header_rows + spec.label_rows + spec.note_rows
        problems.extend(
            f"table {index} lists row {row} outside its span {spec.row_span}"
            for row in listed
            if not top <= row <= bottom
        )
        left, right = spec.col_span
        problems.extend(
            f"table {index} lists column {col} outside its span {spec.col_span}"
            for col in spec.header_cols
            if not left <= col <= right
        )
        body = [row for row in range(top, bottom + 1) if row not in set(spec.header_rows + spec.note_rows)]
        if not body:
            problems.append(f"table {index} has no body rows")
    return problems


def _overview(sheet: SheetExtraction) -> str:
    box = _extent(sheet)
    text = render_range(sheet, box)
    if count_tokens(text) <= OVERVIEW_BUDGET:
        return f"whole sheet:\n{text}"
    top, left, bottom, right = box
    return (
        f"sheet is too large to show at once (extent r{top}c{left}:r{bottom}c{right}). "
        f"its shape:\n{outline(sheet)}"
    )


def _step_prompt(sheet: SheetExtraction, transcript: list[str], remaining: set[tuple[int, int]]) -> str:
    return "\n\n".join(
        [
            load_prompt("table_agent"),
            sheet_facts(sheet),
            _overview(sheet),
            *transcript,
            f"unclaimed: {_summarize(remaining)}",
        ]
    )


def next_prompt(sheet: SheetExtraction, state: AgentState, max_steps: int = MAX_STEPS) -> str | None:
    if state.stopped is not None:
        return None
    if state.steps >= max_steps:
        state.stopped = "step limit"
        return None
    if state.idle > MAX_IDLE_STEPS:
        state.stopped = "no progress"
        return None
    remaining = unclaimed(sheet, state.tables, state.dismissed)
    if not remaining:
        state.stopped = "complete"
        return None
    return _step_prompt(sheet, state.transcript, remaining)


def _do_outline(sheet: SheetExtraction, state: AgentState, action: dict) -> tuple[str, bool]:
    return f"outline\n{outline(sheet)}", False


def _do_read(sheet: SheetExtraction, state: AgentState, action: dict) -> tuple[str, bool]:
    top, left, bottom, right = action["range"]
    body = render_range(sheet, (top, left, bottom, right), READ_BUDGET)
    return f"read r{top}c{left}:r{bottom}c{right}\n{body}", False


def _do_rows_where(sheet: SheetExtraction, state: AgentState, action: dict) -> tuple[str, bool]:
    column, holds = action["column"], action["holds"]
    return f"rows_where c{column} {holds}\n{rows_where(sheet, column, holds)}", False


def _do_emit(sheet: SheetExtraction, state: AgentState, action: dict) -> tuple[str, bool]:
    state.tables.append(TableSpec.model_validate(action["table"]))
    return f"emitted table {len(state.tables) - 1}: {action['table']}", True


def _do_amend(sheet: SheetExtraction, state: AgentState, action: dict) -> tuple[str, bool]:
    position = action["index"]
    if not 0 <= position < len(state.tables):
        return f"amend failed: no table {position}", False
    state.tables[position] = TableSpec.model_validate(action["table"])
    return f"amended table {position}: {action['table']}", True


def _do_dismiss(sheet: SheetExtraction, state: AgentState, action: dict) -> tuple[str, bool]:
    top, left, bottom, right = action["range"]
    state.dismissed.append((top, left, bottom, right))
    return f"dismissed r{top}c{left}:r{bottom}c{right}", True


_HANDLERS = {
    "outline": _do_outline,
    "read": _do_read,
    "rows_where": _do_rows_where,
    "emit": _do_emit,
    "amend": _do_amend,
    "dismiss": _do_dismiss,
}

_NEEDS = {
    "read": "range",
    "rows_where": "column",
    "emit": "table",
    "amend": "table",
    "dismiss": "range",
}


def apply_action(sheet: SheetExtraction, state: AgentState, action: dict) -> None:
    state.steps += 1
    name = action["action"]
    if name == "done":
        state.stopped = "agent done"
        return
    handler = _HANDLERS.get(name)
    required = _NEEDS.get(name)
    if handler is None or (required is not None and action.get(required) is None):
        state.transcript.append(f"{name} ignored: missing arguments")
        state.idle += 1
        return
    line, progressed = handler(sheet, state, action)
    state.transcript.append(line)
    state.idle = 0 if progressed else state.idle + 1


def finish(sheet: SheetExtraction, state: AgentState) -> SheetResult:
    remaining = unclaimed(sheet, state.tables, state.dismissed)
    problems = verify(sheet, state.tables)
    if remaining:
        problems.append(f"unclaimed {_summarize(remaining)}")
    if state.stopped not in {"complete", "agent done", None}:
        problems.append(f"stopped: {state.stopped}")
    logger.info(
        "agent sheet%d steps=%d tables=%d problems=%d", sheet.sheet_no, state.steps, len(state.tables), len(problems)
    )
    return SheetResult(
        tables=state.tables,
        dismissed=state.dismissed,
        unclaimed=len(remaining),
        steps=state.steps,
        problems=problems,
    )


async def structure_sheet(sheet: SheetExtraction, *, key: str, max_steps: int = MAX_STEPS) -> SheetResult:
    state = AgentState()
    log: list[StepLog] = []
    while True:
        prompt = next_prompt(sheet, state, max_steps)
        if prompt is None:
            break
        capacity = get_text_capacity()
        await capacity.acquire(key)
        try:
            action = await call_structured(prompt, _ACTION_SCHEMA)
        finally:
            await capacity.release(key)
        apply_action(sheet, state, action)
        log.append(StepLog(step=state.steps, prompt=prompt, action=action))
    result = finish(sheet, state)
    result.log = log
    return result
