import json
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from citadel.llm import MODEL_CTX, call_structured, count_tokens
from citadel.tabular.sheet_dump import SheetDump
from citadel.tabular.sheet_structure import (
    SheetTables,
    coverage,
    fill_computed,
    request_schema,
    review_extraction,
    score_extraction,
    validate_extraction,
)

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "sheet_tables.md"
MAX_ROUNDS = 4
MIN_OUTPUT_TOKENS = 2048
MAX_OUTPUT_TOKENS = 16384
RETRY_HEADROOM = 4096


@lru_cache(maxsize=1)
def system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def input_tokens(dump_text: str) -> int:
    return count_tokens(system_prompt()) + count_tokens(dump_text)


def output_budget(dump_text: str) -> int:
    free = MODEL_CTX - input_tokens(dump_text) - RETRY_HEADROOM
    return max(MIN_OUTPUT_TOKENS, min(MAX_OUTPUT_TOKENS, free))


def build_prompt(dump_text: str, attempt: str | None, problems: list[str], notes: list[str]) -> str:
    parts = [system_prompt(), dump_text]
    if attempt is None:
        return "\n\n".join(parts)
    parts.append("## Your previous answer\n\n" + attempt)
    if problems:
        parts.append(
            "## These must be fixed\n\n"
            + "\n".join(f"- {problem}" for problem in problems)
            + "\n\nEvery row in the extent must be covered exactly once."
        )
    if notes:
        parts.append(
            "## These may or may not be problems\n\n"
            + "\n".join(f"- {note}" for note in notes)
            + "\n\nJudge each for yourself against the sheet. Keep your answer as it is where you disagree."
        )
    parts.append("Return a SheetTables.")
    return "\n\n".join(parts)


def fits_context(dump: SheetDump, dump_text: str) -> tuple[bool, int, int]:
    needed = input_tokens(dump_text)
    allowed = MODEL_CTX - MIN_OUTPUT_TOKENS - RETRY_HEADROOM
    return needed <= allowed, needed, allowed


def accepted(dump: SheetDump, extraction: SheetTables, rounds: int) -> SheetTables:
    extraction.rounds = rounds
    tally = score_extraction(dump, extraction)
    logger.info(
        "extracted %d tables from %s/%s coverage=%.3f rounds=%d support=%s",
        len(extraction.tables),
        dump.workbook,
        dump.sheet,
        coverage(dump, extraction),
        rounds,
        tally,
    )
    return extraction


async def extract_sheet(dump: SheetDump, dump_text: str, max_rounds: int = MAX_ROUNDS) -> SheetTables:
    fits, needed, allowed = fits_context(dump, dump_text)
    if not fits:
        message = f"{dump.workbook}/{dump.sheet} needs {needed} input tokens, budget is {allowed}"
        raise ValueError(message)
    schema = request_schema()
    budget = output_budget(dump_text)
    attempt: str | None = None
    problems: list[str] = []
    notes: list[str] = []
    seen_notes = False
    best: SheetTables | None = None
    for round_index in range(max_rounds):
        prompt = build_prompt(dump_text, attempt, problems, notes)
        try:
            raw = await call_structured(prompt, schema, max_tokens=budget)
            extraction = fill_computed(dump, SheetTables.model_validate(raw))
        except json.JSONDecodeError:
            attempt = None
            problems = [f"the response was not valid JSON, most likely truncated at {budget} tokens"]
            logger.info("round %d unparseable output for %s", round_index, dump.sheet)
            continue
        except ValidationError as exc:
            attempt = None
            problems = [f"the response did not match the schema: {exc.error_count()} errors"]
            logger.info("round %d schema mismatch for %s", round_index, dump.sheet)
            continue
        if best is None or extraction.tables or not best.tables:
            best = extraction
        problems = validate_extraction(dump, extraction)
        if not problems and not extraction.tables and best.tables:
            logger.info("keeping earlier answer for %s: revision dropped every table", dump.sheet)
            return accepted(dump, best, round_index + 1)
        notes = [] if seen_notes else review_extraction(extraction)
        seen_notes = seen_notes or bool(notes)
        if not problems and not notes:
            return accepted(dump, extraction, round_index + 1)
        attempt = extraction.model_dump_json()
        logger.info(
            "round %d revising %s/%s: %s",
            round_index,
            dump.workbook,
            dump.sheet,
            "; ".join([*problems, *notes][:3]),
        )
    logger.warning(
        "no valid extraction for %s/%s after %d rounds, reporting sheet as unresolved: %s",
        dump.workbook,
        dump.sheet,
        max_rounds,
        "; ".join(problems[:3]),
    )
    fallback = best or SheetTables(workbook=dump.workbook, sheet=dump.sheet)
    fallback.unresolved = [f"{dump.extent} unvalidated: {problem}" for problem in problems[:5]]
    fallback.rounds = max_rounds
    score_extraction(dump, fallback)
    return fallback
