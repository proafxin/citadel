import asyncio
import functools
import json

import httpx

from citadel.prompts import load_prompt
from citadel.schemas.table import Column
from config import get_settings

SLM_TIMEOUT = 180


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


async def _chat(prompt: str, fmt: dict | str) -> str:
    payload = {
        "model": get_settings().qwen_model,
        "messages": [{"role": "user", "content": prompt}],
        "think": False,
        "stream": False,
        "format": fmt,
        "options": {"temperature": 0},
    }
    async with _slm_semaphore(), httpx.AsyncClient(timeout=SLM_TIMEOUT) as client:
        response = await client.post(_ollama_chat_url(), json=payload)
        response.raise_for_status()
        return response.json()["message"]["content"]


async def call_slm(prompt: str, schema: dict) -> dict:
    return json.loads(_extract_json(await _chat(prompt, schema)))


_DESCRIPTION_SCHEMA = {
    "type": "object",
    "properties": {"description": {"type": "string"}},
    "required": ["description"],
}


async def describe_table(columns: list[Column], sample_rows: list[list], context: str) -> str:
    header = " | ".join(column.header or f"col{index}" for index, column in enumerate(columns))
    rows = "\n".join(" | ".join("" if value is None else str(value) for value in row) for row in sample_rows)
    prompt = f"{load_prompt('table_description')}\nsource: {context}\ncolumns: {header}\nsample rows:\n{rows}"
    data = await call_slm(prompt, _DESCRIPTION_SCHEMA)
    return str(data.get("description", ""))
