import json
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from citadel.llm import MODEL_CTX, call_structured, count_tokens
from citadel.tabular.sheet_dump import SheetDump
from citadel.tabular.sheet_structure import SheetExtraction, coverage, validate_extraction

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "sheet_tables.md"
MAX_ROUNDS = 4
BASE_OUTPUT_TOKENS = 4096
TOKENS_PER_COLUMN = 128
MAX_OUTPUT_TOKENS = 16384
RETRY_HEADROOM = 4096


@lru_cache(maxsize=1)
def system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def output_budget(dump: SheetDump) -> int:
    estimate = BASE_OUTPUT_TOKENS + TOKENS_PER_COLUMN * max(len(dump.columns), 1)
    return min(estimate, MAX_OUTPUT_TOKENS)


def build_prompt(dump_text: str, attempt: str | None, problems: list[str]) -> str:
    parts = [system_prompt(), dump_text]
    if attempt is not None:
        parts.extend(
            [
                "## Your previous answer\n\n" + attempt,
                "## Validation failures\n\n"
                + "\n".join(f"- {problem}" for problem in problems)
                + "\n\nReturn a corrected SheetExtraction. Every row in the extent must be covered exactly once.",
            ]
        )
    return "\n\n".join(parts)


def input_budget(dump: SheetDump) -> int:
    return MODEL_CTX - output_budget(dump) - RETRY_HEADROOM


def fits_context(dump: SheetDump, dump_text: str) -> tuple[bool, int, int]:
    needed = count_tokens(system_prompt()) + count_tokens(dump_text)
    allowed = input_budget(dump)
    return needed <= allowed, needed, allowed


async def extract_sheet(dump: SheetDump, dump_text: str, max_rounds: int = MAX_ROUNDS) -> SheetExtraction:
    fits, needed, allowed = fits_context(dump, dump_text)
    if not fits:
        message = f"{dump.workbook}/{dump.sheet} needs {needed} input tokens, budget is {allowed}"
        raise ValueError(message)
    schema = SheetExtraction.model_json_schema()
    budget = output_budget(dump)
    attempt: str | None = None
    problems: list[str] = []
    for round_index in range(max_rounds):
        prompt = build_prompt(dump_text, attempt, problems)
        try:
            raw = await call_structured(prompt, schema, max_tokens=budget)
            extraction = SheetExtraction.model_validate(raw)
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
        problems = validate_extraction(dump, extraction)
        if not problems:
            logger.info(
                "extracted %d tables from %s/%s coverage=%.3f rounds=%d",
                len(extraction.tables),
                dump.workbook,
                dump.sheet,
                coverage(dump, extraction),
                round_index + 1,
            )
            return extraction
        attempt = extraction.model_dump_json()
        logger.info(
            "round %d rejected %s/%s: %s", round_index, dump.workbook, dump.sheet, "; ".join(problems[:3])
        )
    message = f"no valid extraction for {dump.workbook}/{dump.sheet} after {max_rounds} rounds"
    raise RuntimeError(message)
