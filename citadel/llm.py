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

MODEL_CTX = 65536
OCR_CTX = 16384
STRUCT_MAX_TOKENS = 4096
STRUCTURE_MAX_TOKENS = 2048
SYNTH_MAX_TOKENS = 8192
SLM_MODEL_LEN = MODEL_CTX
PAGE_OCR_MAX_TOKENS = 8192
OCR_CEILING_MARGIN = 16
RELEVANCE_BUDGET = SLM_MODEL_LEN - STRUCT_MAX_TOKENS - 2048


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
    path = hf_hub_download(QWEN_HF_REPO, "tokenizer.json", cache_dir=str(QWEN_CACHE_DIR), local_files_only=True)
    return Tokenizer.from_file(path)


def count_tokens(text: str) -> int:
    return len(get_tokenizer().encode(text, add_special_tokens=False).ids)


def count_tokens_batch(texts: list[str]) -> list[int]:
    if not texts:
        return []
    return [len(encoding.ids) for encoding in get_tokenizer().encode_batch(texts, add_special_tokens=False)]


def extract_json(text: str) -> str:
    spans: list[str] = []
    depth = 0
    start = -1
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append(text[start : index + 1])
    for span in reversed(spans):
        try:
            json.loads(span)
        except json.JSONDecodeError:
            continue
        return span
    return text


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
        "reasoning_effort": "none",
    }


def _text_payload(prompt: str, max_tokens: int) -> dict:
    return {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "reasoning_effort": "none",
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
    return json.loads(extract_json(raw))


async def call_text(prompt: str, max_tokens: int, key: str) -> str:
    cap = get_text_large_capacity()
    await cap.acquire(key)
    try:
        return (await _post_qwen(_text_payload(prompt, max_tokens))).strip()
    finally:
        await cap.release(key)


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
        "reasoning_effort": "none",
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
    text = "".join(parts).strip()
    produced = count_tokens(text)
    if produced >= max_tokens - OCR_CEILING_MARGIN:
        logger.warning("page ocr hit token ceiling key=%s produced=%d max=%d", image_key, produced, max_tokens)
    return text, wait_s, gpu_s


async def call_structured(prompt: str, schema: dict, max_tokens: int = STRUCTURE_MAX_TOKENS) -> dict:
    raw = await _post_qwen(_struct_payload(prompt, _inline_refs(schema), max_tokens))
    try:
        return json.loads(extract_json(raw))
    except json.JSONDecodeError:
        logger.exception("call_structured got unparseable output max_tokens=%d raw=%r", max_tokens, raw)
        raise


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

_STAGE1_SCHEMA = {
    "type": "object",
    "properties": {"documents": {"type": "array", "items": {"type": "integer"}}},
    "required": ["documents"],
}

_LEVEL1_SCHEMA = {
    "type": "object",
    "properties": {
        "batches": {"type": "array", "items": {"type": "integer"}},
        "tables": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["batches", "tables"],
}

_LEVEL2_SCHEMA = {
    "type": "object",
    "properties": {"relevant": {"type": "array", "items": {"type": "integer"}}},
    "required": ["relevant"],
}


def _indices(data: dict, key: str, count: int) -> list[int]:
    return sorted({index for index in data.get(key, []) if isinstance(index, int) and 0 <= index < count})


def _inventory_prompt(prompt_name: str, query: str, items: list[str]) -> str:
    listing = "\n\n".join(f"[{index}] {item}" for index, item in enumerate(items))
    return f"{load_prompt(prompt_name)}\nquestion: {query}\ninventory:\n{listing}"


def relevance_prompt_tokens(prompt_name: str, query: str, items: list[str]) -> int:
    return count_tokens(_inventory_prompt(prompt_name, query, items))


async def call_stage1_relevance(query: str, items: list[str], key: str) -> list[int]:
    prompt = _inventory_prompt("stage1_relevance", query, items)
    tokens = count_tokens(prompt)
    if tokens > RELEVANCE_BUDGET:
        logger.warning("stage1 inventory does not fit one call tokens=%d budget=%d", tokens, RELEVANCE_BUDGET)
    data = await call_slm(prompt, _STAGE1_SCHEMA, key)
    relevant = _indices(data, "documents", len(items))
    logger.info("stage1 items=%d relevant=%d", len(items), len(relevant))
    return relevant


async def call_level1_relevance(query: str, items: list[str], key: str) -> tuple[list[int], list[int]]:
    prompt = _inventory_prompt("level1_relevance", query, items)
    tokens = count_tokens(prompt)
    if tokens > RELEVANCE_BUDGET:
        logger.warning("level1 inventory does not fit one call tokens=%d budget=%d", tokens, RELEVANCE_BUDGET)
    data = await call_slm(prompt, _LEVEL1_SCHEMA, key)
    batches = _indices(data, "batches", len(items))
    tables = _indices(data, "tables", len(items))
    logger.info("level1 items=%d relevant_batches=%d relevant_tables=%d", len(items), len(batches), len(tables))
    return batches, tables


async def call_level2_relevance(query: str, excerpts: list[str], key: str) -> list[int]:
    prompt = f"{load_prompt('level2_relevance')}\nquestion: {query}\nexcerpts:\n" + "\n\n".join(
        f"[{index}] {excerpt}" for index, excerpt in enumerate(excerpts)
    )
    data = await call_slm(prompt, _LEVEL2_SCHEMA, key)
    return _indices(data, "relevant", len(excerpts))


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
        "temperature": 0.7,
        "top_p": 0.80,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
        "max_tokens": SYNTH_MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
        "reasoning_effort": "none",
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


async def write_final_report(query: str, text_report: str, table_report: str) -> AsyncIterator[str]:
    prompt = (
        f"{load_prompt('final_report')}\nquestion: {query}\n"
        f"document report:\n{text_report}\n\ntable report:\n{table_report}"
    )
    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "top_p": 0.80,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
        "max_tokens": SYNTH_MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
        "reasoning_effort": "none",
        "stream": True,
    }
    key = _local_key("final_report")
    cap = get_interactive_capacity()
    await cap.acquire(key)
    try:
        async for token in _stream_qwen(payload):
            yield token
    finally:
        await cap.release(key)
