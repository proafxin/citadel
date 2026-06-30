import asyncio
import functools
import json
import logging

import httpx

from citadel.prompts import load_prompt
from citadel.schemas.tree import HeadingInfo
from config import get_settings

logger = logging.getLogger(__name__)


@functools.lru_cache
def _ollama_chat_url() -> str:
    settings = get_settings()
    return f"http://{settings.qwen_host}:{settings.qwen_port}/api/chat"


@functools.lru_cache
def _slm_semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(get_settings().slm_concurrency)


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end != -1 else text


async def level_headings(headings: list[HeadingInfo]) -> dict[int, int]:
    if not headings:
        return {}
    fonts = sorted({round(h.font_size, 1) for h in headings if h.font_size})
    logger.info("leveling %d headings; distinct font sizes=%s", len(headings), fonts)
    rows: list[str] = []
    for index, info in enumerate(headings):
        size = round(info.font_size, 1) if info.font_size else "?"
        rows.append(f"{index} | size={size} | p{info.page} | {info.text}")
        rows.append(f"    intro: {info.context}" if info.context else "    intro: (no text directly under it)")
    lines = "\n".join(rows)
    payload = {
        "model": get_settings().qwen_model,
        "messages": [{"role": "user", "content": f"{load_prompt('heading_levels')}\n{lines}"}],
        "think": False,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    async with _slm_semaphore():
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(_ollama_chat_url(), json=payload)
                response.raise_for_status()
        except httpx.HTTPError:
            logger.warning("SLM unreachable for heading levels; falling back to pattern")
            return {}
        content = response.json()["message"]["content"]
    data = json.loads(_extract_json(content))
    levels = {int(key): int(value) for key, value in data.items() if key.isdigit()} if isinstance(data, dict) else {}
    if len(levels) < len(headings):
        logger.info("SLM leveled %d/%d headings; pattern fills the gaps", len(levels), len(headings))
    return levels
