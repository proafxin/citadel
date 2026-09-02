import logging

from pydantic import BaseModel

from citadel.llm import count_tokens, count_tokens_batch
from citadel.tabular.sheet_agent import input_allowance, system_prompt
from citadel.tabular.sheet_dump import SheetDump

logger = logging.getLogger(__name__)

EXPLORE_RESERVE = 8192
MIN_WINDOW_TOKENS = 2048


class Window(BaseModel):
    index: int
    first_row: int
    last_row: int
    tokens: int


def row_costs(dump: SheetDump) -> dict[int, int]:
    rows = sorted(dump.lines)
    return dict(zip(rows, count_tokens_batch([dump.lines[row] for row in rows]), strict=True))


def overhead_tokens(dump_text: str, costs: dict[int, int]) -> int:
    return max(count_tokens(dump_text) - sum(costs.values()), 0)


def window_budget() -> int:
    return input_allowance() - count_tokens(system_prompt()) - EXPLORE_RESERVE


def plan_windows(dump: SheetDump, dump_text: str, budget: int | None = None) -> list[Window]:
    costs = row_costs(dump)
    if not costs:
        return []
    allowed = budget if budget is not None else window_budget()
    room = max(allowed - overhead_tokens(dump_text, costs), MIN_WINDOW_TOKENS)
    windows: list[Window] = []
    first = 0
    last = 0
    spent = 0
    for row in sorted(costs):
        cost = costs[row]
        if first and spent + cost > room:
            windows.append(Window(index=len(windows) + 1, first_row=first, last_row=last, tokens=spent))
            first = 0
            spent = 0
        if not first:
            first = row
        last = row
        spent += cost
    if first:
        windows.append(Window(index=len(windows) + 1, first_row=first, last_row=last, tokens=spent))
    logger.info(
        "%s/%s planned %d window(s) over rows %d-%d with room %d",
        dump.workbook,
        dump.sheet,
        len(windows),
        dump.first_row,
        dump.last_row,
        room,
    )
    return windows
