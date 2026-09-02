import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from citadel.llm import MODEL_CTX, call_structured, call_text, count_tokens, extract_json
from citadel.tabular.sheet_dump import SheetDump
from citadel.tabular.sheet_structure import (
    Draft,
    advise_structure,
    SheetStructure,
    coverage,
    fill_computed,
    request_schema,
    review_draft,
    review_structure,
    score_structure,
)
from citadel.tabular.sheet_tools import ToolError, dispatch, render_result

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "sheet_tables.md"
SERIALISE_PATH = Path(__file__).resolve().parents[2] / "prompts" / "sheet_serialise.md"
MAX_ROUNDS = 4
MAX_TOOL_CALLS = 12
MAX_TOOL_ROUNDS = 4
MIN_OUTPUT_TOKENS = 2048
MAX_OUTPUT_TOKENS = 16384
WINDOW_OUTPUT_TOKENS = 4096
EXPLORE_MAX_TOKENS = 8192
RETRY_HEADROOM = 4096
MIN_ANALYSIS_CHARS = 200
ANALYSIS_MAX_TOKENS = 4096
TOOL_BLOCK = re.compile(r"```tools?\s*(.+?)```", re.DOTALL)


class ExploreRound(BaseModel):
    index: int
    prompt_tokens: int
    response: str
    tool_calls: list[dict] = Field(default_factory=list)
    tool_results: str = ""
    findings: list[str] = Field(default_factory=list)


class CommitAttempt(BaseModel):
    index: int
    budget: int
    error: str = ""
    findings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AgentTrace(BaseModel):
    workbook: str = ""
    sheet: str = ""
    first_row: int = 0
    last_row: int = 0
    rounds: list[ExploreRound] = Field(default_factory=list)
    draft: Draft | None = None
    transcript_tokens: int = 0
    analysis_tokens: int = 0
    commits: list[CommitAttempt] = Field(default_factory=list)


@lru_cache(maxsize=1)
def system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def serialise_prompt() -> str:
    return SERIALISE_PATH.read_text(encoding="utf-8")


def input_tokens(dump_text: str) -> int:
    return count_tokens(system_prompt()) + count_tokens(dump_text)


def output_budget(prompt: str) -> int:
    free = MODEL_CTX - count_tokens(prompt) - RETRY_HEADROOM
    return max(MIN_OUTPUT_TOKENS, min(MAX_OUTPUT_TOKENS, free))


def input_allowance() -> int:
    return MODEL_CTX - WINDOW_OUTPUT_TOKENS - RETRY_HEADROOM


def fits_context(dump: SheetDump, dump_text: str) -> tuple[bool, int, int]:
    needed = input_tokens(dump_text)
    allowed = input_allowance()
    return needed <= allowed, needed, allowed


def tool_requests(response: str) -> list[dict]:
    match = TOOL_BLOCK.search(response)
    if match is None:
        return []
    payload = json.loads(extract_json(match.group(1)))
    calls = payload.get("tool_calls", payload) if isinstance(payload, dict) else payload
    return calls if isinstance(calls, list) else [calls]


def run_tools(dump: SheetDump, calls: list[dict]) -> str:
    lines: list[str] = []
    for call in calls:
        name = str(call.get("name", ""))
        arguments = call.get("arguments") or {}
        try:
            lines.append(f"### {name} {json.dumps(arguments)}\n{render_result(dispatch(dump, name, arguments))}")
        except ToolError as exc:
            lines.append(f"### {name}\n{exc}")
        except (TypeError, ValueError) as exc:
            lines.append(f"### {name}\n{name} could not run with those arguments: {exc}")
    return "\n\n".join(lines)


def read_draft(response: str) -> tuple[Draft | None, str | None]:
    try:
        return Draft.model_validate(json.loads(extract_json(response))), None
    except json.JSONDecodeError:
        return None, "no summary block was found; end your answer with the regions and blocks as JSON"
    except ValidationError as exc:
        return None, f"the summary block did not have the expected shape: {exc.error_count()} errors"


def trim_analysis(transcript: str, dump_text: str, draft: Draft | None = None) -> str:
    room = min(
        ANALYSIS_MAX_TOKENS,
        MODEL_CTX
        - count_tokens(serialise_prompt())
        - count_tokens(dump_text)
        - MIN_OUTPUT_TOKENS
        - RETRY_HEADROOM,
    )
    if room <= 0:
        return ""
    if count_tokens(transcript) <= room:
        return transcript
    head = "" if draft is None else "## Regions and roles decided\n\n" + draft.model_dump_json() + "\n\n"
    tail = max(room - count_tokens(head), 0)
    kept = transcript
    while len(kept) > MIN_ANALYSIS_CHARS and count_tokens(kept) > tail:
        kept = kept[len(kept) // 4 :]
    return f"{head}[earlier reasoning omitted]\n\n{kept}"


async def explore(
    dump: SheetDump, dump_text: str, max_rounds: int, trace: AgentTrace | None = None
) -> tuple[str, Draft | None]:
    head = f"{system_prompt()}\n\n{dump_text}"
    exchange: list[str] = []
    spent = 0
    tool_rounds = 0
    drafts = 0
    index = 0
    draft: Draft | None = None
    while drafts < max_rounds and index < max_rounds + MAX_TOOL_ROUNDS:
        prompt = "\n\n".join([head, *exchange])
        response = await call_text(prompt, EXPLORE_MAX_TOKENS, f"sheet-explore:{dump.workbook}:{dump.sheet}")
        exchange.append(response)
        record = ExploreRound(index=index, prompt_tokens=count_tokens(prompt), response=response)
        index += 1
        if trace is not None:
            trace.rounds.append(record)
        calls = tool_requests(response)
        if calls and spent < MAX_TOOL_CALLS and tool_rounds < MAX_TOOL_ROUNDS:
            allowed = calls[: MAX_TOOL_CALLS - spent]
            spent += len(allowed)
            tool_rounds += 1
            results = run_tools(dump, allowed)
            record.tool_calls = allowed
            record.tool_results = results
            exchange.append("## Tool results\n\n" + results)
            continue
        drafts += 1
        found, problem = read_draft(response)
        draft = found or draft
        findings = [problem] if problem else review_draft(dump, found) if found else []
        record.findings = findings
        if not findings:
            logger.info(
                "explored %s/%s in %d draft(s), %d tool round(s), %d tool calls",
                dump.workbook,
                dump.sheet,
                drafts,
                tool_rounds,
                spent,
            )
            return "\n\n".join(exchange), draft
        exchange.append("## Review these\n\n" + "\n".join(f"- {finding}" for finding in findings))
        logger.info("draft %d revising %s: %s", drafts, dump.sheet, "; ".join(findings[:2]))
    return "\n\n".join(exchange), draft


async def extract_sheet(
    dump: SheetDump, dump_text: str, max_rounds: int = MAX_ROUNDS, trace: AgentTrace | None = None
) -> SheetStructure:
    fits, needed, allowed = fits_context(dump, dump_text)
    if not fits:
        message = f"{dump.workbook}/{dump.sheet} needs {needed} input tokens, budget is {allowed}"
        raise ValueError(message)
    transcript, draft = await explore(dump, dump_text, max_rounds, trace)
    analysis = trim_analysis(transcript, dump_text, draft)
    if trace is not None:
        trace.workbook = dump.workbook
        trace.sheet = dump.sheet
        trace.first_row = dump.first_row
        trace.last_row = dump.last_row
        trace.draft = draft
        trace.transcript_tokens = count_tokens(transcript)
        trace.analysis_tokens = count_tokens(analysis)
    prompt = "\n\n".join([serialise_prompt(), dump_text, "## Analysis\n\n" + analysis, "Return a SheetStructure."])
    schema = request_schema()
    findings: list[str] = []
    best: SheetStructure | None = None
    for attempt in range(max_rounds):
        budget = output_budget(prompt)
        commit = CommitAttempt(index=attempt, budget=budget)
        if trace is not None:
            trace.commits.append(commit)
        try:
            raw = await call_structured(prompt, schema, max_tokens=budget)
            structure = fill_computed(dump, SheetStructure.model_validate(raw))
        except json.JSONDecodeError:
            commit.error = "truncated"
            prompt = (
                f"{prompt}\n\nThe last answer ran past {budget} tokens and was cut off. Leave out every "
                "ColumnDef whose name the worksheet does not give, and omit group and header_parts where "
                "they are empty."
            )
            continue
        except ValidationError as exc:
            commit.error = f"schema: {exc.error_count()} errors"
            prompt = f"{prompt}\n\nThe last answer did not match the schema: {exc.error_count()} errors."
            continue
        if best is None or structure.regions:
            best = structure
        findings = review_structure(dump, structure)
        notes = advise_structure(dump, structure)
        commit.findings = findings
        commit.notes = notes
        if not findings:
            structure.rounds = attempt + 1
            structure.unresolved = notes[:5]
            score_structure(dump, structure)
            logger.info(
                "structured %s/%s into %d regions, %d metadata, coverage=%.3f, notes=%d",
                dump.workbook,
                dump.sheet,
                len(structure.regions),
                len(structure.metadata),
                coverage(dump, structure),
                len(notes),
            )
            return structure
        prompt = f"{prompt}\n\n## Review these\n\n" + "\n".join(f"- {item}" for item in [*findings, *notes])
    logger.warning("%s/%s unresolved: %s", dump.workbook, dump.sheet, "; ".join(findings[:3]))
    fallback = best or SheetStructure(workbook=dump.workbook, sheet=dump.sheet)
    fallback.unresolved = findings[:5]
    fallback.rounds = max_rounds
    score_structure(dump, fallback)
    return fallback
