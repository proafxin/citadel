import functools
import itertools
import json
import logging
import time
from base64 import b64encode
from collections.abc import AsyncIterator

import httpx
from huggingface_hub import hf_hub_download
from tokenizers import Tokenizer

from citadel.bus import get_redis
from citadel.prompts import load_prompt
from citadel.schemas.query import QueryPlan
from citadel.services.capacity import (
    get_embed_capacity,
    get_interactive_capacity,
    get_text_large_capacity,
)
from config import EMBED_SERVED_NAME, QWEN_CACHE_DIR, QWEN_HF_REPO, QWEN_MODEL, get_settings

logger = logging.getLogger(__name__)

_call_no = itertools.count()

NO_TIMEOUT = httpx.Timeout(None)

STRUCT_MAX_TOKENS = 4096
STRUCTURE_MAX_TOKENS = 8192
SYNTH_MAX_TOKENS = 8192
SLM_MODEL_LEN = 32768
PAGE_OCR_MAX_TOKENS = 3584
RESOLVE_BUDGET = SLM_MODEL_LEN - STRUCT_MAX_TOKENS - 2048


@functools.lru_cache
def _qwen_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=get_settings().qwen_base_url, timeout=NO_TIMEOUT)


@functools.lru_cache
def _bge_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=get_settings().bge_base_url, timeout=NO_TIMEOUT)


def _local_key(label: str) -> str:
    return f"llm:{label}:{next(_call_no)}"


@functools.lru_cache
def get_tokenizer() -> Tokenizer:
    path = hf_hub_download(QWEN_HF_REPO, "tokenizer.json", cache_dir=str(QWEN_CACHE_DIR))
    return Tokenizer.from_file(path)


def count_tokens(text: str) -> int:
    return len(get_tokenizer().encode(text, add_special_tokens=False).ids)


def count_tokens_batch(texts: list[str]) -> list[int]:
    if not texts:
        return []
    return [len(encoding.ids) for encoding in get_tokenizer().encode_batch(texts, add_special_tokens=False)]


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end != -1 else text


def _resolve_ref(node: object, defs: dict) -> object:
    if isinstance(node, dict):
        if "$ref" in node:
            return _resolve_ref(defs[node["$ref"].split("/")[-1]], defs)
        return {key: _resolve_ref(value, defs) for key, value in node.items() if key != "$defs"}
    if isinstance(node, list):
        return [_resolve_ref(item, defs) for item in node]
    return node


def _inline_refs(schema: dict) -> dict:
    defs = schema.get("$defs")
    if not defs:
        return schema
    resolved = _resolve_ref(schema, defs)
    return resolved if isinstance(resolved, dict) else schema


def _struct_payload(prompt: str, schema: dict, max_tokens: int = STRUCT_MAX_TOKENS) -> dict:
    return {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}},
        "chat_template_kwargs": {"enable_thinking": False},
    }


def _text_payload(prompt: str, max_tokens: int) -> dict:
    return {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }


async def _post_qwen(payload: dict) -> str:
    response = await _qwen_client().post("/chat/completions", json=payload)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


async def _stream_qwen(payload: dict) -> AsyncIterator[str]:
    async with _qwen_client().stream("POST", "/chat/completions", json=payload) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: ") :]
            if data == "[DONE]":
                break
            delta = json.loads(data)["choices"][0]["delta"].get("content")
            if delta:
                yield delta


async def call_slm(prompt: str, schema: dict, key: str, max_tokens: int = STRUCT_MAX_TOKENS) -> dict:
    cap = get_interactive_capacity()
    await cap.acquire(key)
    try:
        raw = await _post_qwen(_struct_payload(prompt, _inline_refs(schema), max_tokens))
    finally:
        await cap.release(key)
    return json.loads(_extract_json(raw))


async def call_text(prompt: str, max_tokens: int, key: str) -> str:
    cap = get_text_large_capacity()
    await cap.acquire(key)
    try:
        return (await _post_qwen(_text_payload(prompt, max_tokens))).strip()
    finally:
        await cap.release(key)


async def call_text_table(prompt: str, max_tokens: int) -> str:
    return (await _post_qwen(_text_payload(prompt, max_tokens))).strip()


async def call_embed(texts: list[str], key: str) -> list[list[float]]:
    cap = get_embed_capacity()
    await cap.acquire(key)
    try:
        response = await _bge_client().post("/embeddings", json={"model": EMBED_SERVED_NAME, "input": texts})
        response.raise_for_status()
        return [item["embedding"] for item in response.json()["data"]]
    finally:
        await cap.release(key)


async def call_page_ocr(image_key: str, max_tokens: int = PAGE_OCR_MAX_TOKENS) -> tuple[str, float, float]:
    data = await get_redis().get(image_key)
    if isinstance(data, str):
        data = data.encode()
    image_url = "data:image/png;base64," + b64encode(data).decode()
    payload = {
        "model": QWEN_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": load_prompt("page_ocr")},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": True,
    }
    started = time.time()
    first_token: float | None = None
    parts: list[str] = []
    async for token in _stream_qwen(payload):
        if first_token is None:
            first_token = time.time()
        parts.append(token)
    done = time.time()
    wait_s = (first_token or done) - started
    gpu_s = done - (first_token or done)
    return "".join(parts).strip(), wait_s, gpu_s


async def call_structure_single(payload: str) -> str:
    prompt = f"{load_prompt('table_structure_single')}\n{payload}"
    return await call_text_table(prompt, STRUCTURE_MAX_TOKENS)


async def call_structure_candidates(payload: str, prompt_name: str) -> str:
    prompt = f"{load_prompt(prompt_name)}\n{payload}"
    return await call_text_table(prompt, STRUCTURE_MAX_TOKENS)


async def merge_evidence(query: str, items: list[str]) -> str:
    listing = "\n\n".join(f"[{index}] {item}" for index, item in enumerate(items))
    prompt = f"{load_prompt('evidence_merge')}\nquestion: {query}\npassages:\n{listing}"
    key = _local_key("merge")
    MERGE_SCHEMA = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }
    data = await call_slm(prompt, MERGE_SCHEMA, key)
    return str(data.get("summary", ""))


_QUERIES_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
}

_RESOLVE_SCHEMA = {
    "type": "object",
    "properties": {
        "documents": {
            "type": "object",
            "properties": {
                "overview": {"type": "array", "items": {"type": "integer"}},
                "parts": {"type": "array", "items": {"type": "integer"}},
                "wording": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["overview", "parts", "wording"],
        },
        "tables": {
            "type": "object",
            "properties": {
                "metadata": {"type": "array", "items": {"type": "integer"}},
                "data": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["metadata", "data"],
        },
    },
    "required": ["documents", "tables"],
}

_DOC_DEPTHS = ("wording", "parts", "overview")
_TABLE_DEPTHS = ("data", "metadata")


def _depth_indices(data: dict, key: str, count: int) -> list[int]:
    return [index for index in data.get(key, []) if isinstance(index, int) and 0 <= index < count]


def _kind_coverage(node: dict, depths: tuple[str, ...], count: int) -> dict[int, str]:
    coverage: dict[int, str] = {}
    for depth in depths:
        for index in _depth_indices(node, depth, count):
            coverage.setdefault(index, depth)
    return coverage


def _resolve_prompt(query: str, items: list[str], library: str) -> str:
    listing = "\n\n".join(f"[{index}] {item}" for index, item in enumerate(items))
    return f"{load_prompt('resolve_query')}\nlibrary: {library}\nquestion: {query}\ninventory:\n{listing}"


def resolve_prompt_tokens(query: str, items: list[str], library: str = "") -> int:
    return count_tokens(_resolve_prompt(query, items, library))


async def call_resolve(
    query: str, items: list[str], key: str, library: str = ""
) -> tuple[dict[int, str], dict[int, str]]:
    prompt = _resolve_prompt(query, items, library)
    tokens = count_tokens(prompt)
    if tokens > RESOLVE_BUDGET:
        logger.warning("resolve inventory does not fit one call tokens=%d budget=%d", tokens, RESOLVE_BUDGET)
    data = await call_slm(prompt, _RESOLVE_SCHEMA, key)
    count = len(items)
    doc_coverage = _kind_coverage(data.get("documents", {}), _DOC_DEPTHS, count)
    table_coverage = _kind_coverage(data.get("tables", {}), _TABLE_DEPTHS, count)
    logger.info(
        "resolve items=%d covered=%d %s",
        count,
        len(doc_coverage) + len(table_coverage),
        {depth: sum(1 for value in doc_coverage.values() if value == depth) for depth in _DOC_DEPTHS}
        | {depth: sum(1 for value in table_coverage.values() if value == depth) for depth in _TABLE_DEPTHS},
    )
    if not doc_coverage and not table_coverage:
        logger.warning("resolve covered nothing raw=%s", data)
    return doc_coverage, table_coverage


def pack_indices(counts: list[int], budget: int) -> list[list[int]]:
    groups: list[list[int]] = []
    current: list[int] = []
    used = 0
    for index, cost in enumerate(counts):
        if current and used + cost > budget:
            groups.append(current)
            current, used = [], 0
        current.append(index)
        used += cost
    if current:
        groups.append(current)
    return groups


async def write_queries(query: str, tables: list[str], library: str = "") -> QueryPlan:
    if not tables:
        return QueryPlan()
    listing = "\n\n".join(tables)
    prompt = f"{load_prompt('text_to_sql')}\nlibrary: {library}\nquestion: {query}\ntables:\n{listing}"
    started = time.time()
    key = _local_key("queries")
    data = await call_slm(prompt, _QUERIES_SCHEMA, key)
    queries = [str(sql) for sql in data.get("queries", []) if str(sql).strip()]
    plan = QueryPlan(queries=queries)
    logger.info(
        "write_queries tables=%d prompt_tokens=%d queries=%d %.1fs",
        len(tables),
        count_tokens(prompt),
        len(queries),
        time.time() - started,
    )
    for sql in queries:
        logger.info("  sql: %s", sql)
    if not queries:
        logger.warning("write_queries returned nothing raw=%s", data)
    return plan


async def synthesize(query: str, passages: list[str], results: list[str]) -> AsyncIterator[str]:
    evidence = "passages:\n" + "\n".join(passages) + "\n\ntable results:\n" + "\n".join(results)
    prompt = f"{load_prompt('synthesize')}\nquestion: {query}\n{evidence}"
    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": SYNTH_MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": True,
    }
    key = _local_key("synthesize")
    cap = get_interactive_capacity()
    await cap.acquire(key)
    try:
        async for token in _stream_qwen(payload):
            yield token
    finally:
        await cap.release(key)
