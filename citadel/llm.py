import asyncio
import functools
import json
from collections.abc import AsyncIterator

import httpx

from citadel.prompts import load_prompt
from citadel.schemas.table import Column
from config import QWEN_MODEL, get_settings

SLM_TIMEOUT = 180
SLM_CONCURRENCY = 2  # concurrent ollama SLM calls (heading-leveling); keep <= OLLAMA_NUM_PARALLEL, low so it doesn't starve MinerU OCR
SLM_NUM_CTX = 32768
SLM_MAX_TOKENS = 4096


@functools.lru_cache
def _ollama_chat_url() -> str:
    settings = get_settings()
    return f"http://{settings.qwen_host}:{settings.qwen_port}/api/chat"


@functools.lru_cache
def _slm_semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(SLM_CONCURRENCY)


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end != -1 else text


async def _chat(prompt: str, fmt: dict | str) -> str:
    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "think": False,
        "stream": False,
        "format": fmt,
        "options": {"temperature": 0, "num_ctx": SLM_NUM_CTX, "num_predict": SLM_MAX_TOKENS},
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


async def _chat_stream(prompt: str) -> AsyncIterator[str]:
    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "think": False,
        "stream": True,
        "options": {"temperature": 0.3, "num_ctx": SLM_NUM_CTX, "num_predict": SLM_MAX_TOKENS},
    }
    async with (
        _slm_semaphore(),
        httpx.AsyncClient(timeout=SLM_TIMEOUT) as client,
        client.stream("POST", _ollama_chat_url(), json=payload) as response,
    ):
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line:
                continue
            token = json.loads(line).get("message", {}).get("content", "")
            if token:
                yield token


_REFORMULATE_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
}


async def reformulate(query: str) -> list[str]:
    data = await call_slm(f"{load_prompt('reformulate')}\nquestion: {query}", _REFORMULATE_SCHEMA)
    out: list[str] = []
    seen: set[str] = set()
    for candidate in [query, *data.get("queries", [])]:
        text = str(candidate).strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    return out


_SELECT_SCHEMA = {
    "type": "object",
    "properties": {"relevant": {"type": "array", "items": {"type": "integer"}}},
    "required": ["relevant"],
}


async def select_evidence(query: str, items: list[str]) -> list[int]:
    if not items:
        return []
    listing = "\n\n".join(f"[{index}] {item}" for index, item in enumerate(items))
    data = await call_slm(f"{load_prompt('evidence_filter')}\nquestion: {query}\nevidence:\n{listing}", _SELECT_SCHEMA)
    return [index for index in data.get("relevant", []) if isinstance(index, int) and 0 <= index < len(items)]


_UNIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "selected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "tier": {"type": "integer"}},
                "required": ["id", "tier"],
            },
        }
    },
    "required": ["selected"],
}


async def unify_evidence(query: str, items: list[str]) -> list[tuple[int, int]]:
    if not items:
        return []
    listing = "\n\n".join(f"[{index}] {item}" for index, item in enumerate(items))
    data = await call_slm(f"{load_prompt('evidence_unify')}\nquestion: {query}\nevidence:\n{listing}", _UNIFY_SCHEMA)
    out: list[tuple[int, int]] = []
    for entry in data.get("selected", []):
        index, tier = entry.get("id"), entry.get("tier")
        if isinstance(index, int) and 0 <= index < len(items) and tier in {1, 2}:
            out.append((index, tier))
    return out


_QUERIES_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
}


async def write_queries(query: str, tables: list[str]) -> list[str]:
    if not tables:
        return []
    listing = "\n\n".join(tables)
    data = await call_slm(f"{load_prompt('text_to_sql')}\nquestion: {query}\ntables:\n{listing}", _QUERIES_SCHEMA)
    return [str(sql) for sql in data.get("queries", []) if str(sql).strip()]


async def synthesize(query: str, passages: list[str], results: list[str]) -> AsyncIterator[str]:
    evidence = "passages:\n" + "\n".join(passages) + "\n\ntable results:\n" + "\n".join(results)
    async for token in _chat_stream(f"{load_prompt('synthesize')}\nquestion: {query}\n{evidence}"):
        yield token
