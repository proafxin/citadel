import functools
import json
import logging

import openai

from config import get_settings

logger = logging.getLogger(__name__)

_LEVEL_PROMPT = (
    "You are given a document's headings in reading order, one per line as 'index. text'. "
    "Assign each heading a hierarchy level: 1 = top-level section, 2 = subsection, 3 = sub-subsection, and so on. "
    "Infer nesting from numbering, casing, wording and order; more specific headings get higher numbers. "
    'Respond with JSON {"levels": [...]} containing EXACTLY one integer per heading, in the same order.'
)


@functools.lru_cache
def get_llm() -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(base_url=get_settings().qwen_base_url, api_key="ollama")


async def level_headings(headings: list[str]) -> list[int]:
    if not headings:
        return []
    numbered = "\n".join(f"{index}. {text}" for index, text in enumerate(headings))
    try:
        response = await get_llm().chat.completions.create(
            model=get_settings().qwen_model,
            messages=[{"role": "system", "content": _LEVEL_PROMPT}, {"role": "user", "content": numbered}],
            response_format={"type": "json_object"},
            temperature=0,
        )
    except openai.APIConnectionError:
        logger.warning("SLM unreachable for heading levels; falling back to pattern")
        return []
    levels = json.loads(response.choices[0].message.content or "{}").get("levels", [])
    if len(levels) != len(headings):
        logger.warning("SLM returned %d levels for %d headings; falling back to pattern", len(levels), len(headings))
        return []
    return [int(level) for level in levels]
