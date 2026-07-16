import functools
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from redis.commands.core import AsyncScript
from transformers import AutoTokenizer, PreTrainedTokenizerBase

from citadel.bus import get_redis
from citadel.prompts import load_prompt
from citadel.schemas.table import Column
from config import QWEN_CACHE_DIR, QWEN_HF_REPO, QWEN_MODEL, get_settings

SLM_TIMEOUT = 180
SLM_CONCURRENCY = 3  # concurrent SLM calls; MUST equal qwen --max-num-seqs. above it the excess only queues inside the
# model, where a waiting request pins its whole prompt in the KV cache and buys nothing
STRUCT_MAX_TOKENS = 4096  # structured calls emit short JSON (indices, a concise merge summary, SQL)
SYNTH_MAX_TOKENS = 8192  # the streamed answer; the evidence budget reserves this much of the context window for it


@functools.lru_cache
def get_tokenizer() -> PreTrainedTokenizerBase:
    return AutoTokenizer.from_pretrained(QWEN_HF_REPO, cache_dir=str(QWEN_CACHE_DIR))


def count_tokens(text: str) -> int:
    return len(get_tokenizer().encode(text, add_special_tokens=False))


def count_tokens_batch(texts: list[str]) -> list[int]:
    if not texts:
        return []
    return [len(ids) for ids in get_tokenizer()(texts, add_special_tokens=False)["input_ids"]]


@functools.lru_cache
def _chat_url() -> str:
    return f"{get_settings().qwen_base_url}/chat/completions"


SLM_SLOTS = "slm:slots"  # the pool itself: one list entry per free slot
SLM_SLOTS_READY = "slm:slots:ready"  # separate marker — an EMPTY list does not exist in redis, so the list's own
# existence cannot say whether the pool was filled or is merely all-borrowed

# fill the pool exactly once, whoever gets here first. SET NX is the guard and lua makes check-and-fill atomic, so two
# processes starting together cannot both fill it
_SLM_FILL_LUA = """
if redis.call('SET', KEYS[2], '1', 'NX') then
    for _ = 1, tonumber(ARGV[1]) do redis.call('RPUSH', KEYS[1], '1') end
end
return 1
"""


@functools.lru_cache
def _slm_fill() -> AsyncScript:
    return get_redis().register_script(_SLM_FILL_LUA)


@asynccontextmanager
async def slm_slot() -> AsyncIterator[None]:
    # ONE pool of slots for every SLM request in the system, held in redis because the callers are in different
    # PROCESSES: the worker structures sheets while the app answers queries, and an asyncio.Semaphore bounds only the
    # process it lives in. two per-process semaphores of SLM_CONCURRENCY meant twice SLM_CONCURRENCY could reach a
    # server sized for exactly that, and the excess queues INSIDE the model, pinning its prompt in the KV cache.
    # the SLM is a bottleneck resource, so its concurrency is decided once, globally, by what the GPU can afford.
    #
    # BLPOP blocks on the connection (not the loop) and hands slots out first-come-first-served, so neither process can
    # starve the other. the slot is returned in `finally`, so a raising call frees it; only a hard kill loses one, and
    # the run flushes redis on start.
    redis = get_redis()
    await _slm_fill()(keys=[SLM_SLOTS, SLM_SLOTS_READY], args=[str(SLM_CONCURRENCY)])
    if await redis.blpop(SLM_SLOTS, timeout=SLM_TIMEOUT) is None:
        msg = f"no SLM slot free after {SLM_TIMEOUT}s"
        raise TimeoutError(msg)
    try:
        yield
    finally:
        await redis.lpush(SLM_SLOTS, "1")


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end != -1 else text


async def _chat(prompt: str, schema: dict) -> str:
    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": STRUCT_MAX_TOKENS,
        "response_format": {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    async with slm_slot(), httpx.AsyncClient(timeout=SLM_TIMEOUT) as client:
        response = await client.post(_chat_url(), json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


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


async def call_slm(prompt: str, schema: dict) -> dict:
    return json.loads(_extract_json(await _chat(prompt, _inline_refs(schema))))


_DESCRIPTION_SCHEMA = {
    "type": "object",
    "properties": {"description": {"type": "string"}},
    "required": ["description"],
}


async def describe_table(
    columns: list[Column], sample_rows: list[list], context: str, formulas: list[str] | None = None
) -> str:
    header = " | ".join(column.header or f"col{index}" for index, column in enumerate(columns))
    rows = "\n".join(" | ".join("" if value is None else str(value) for value in row) for row in sample_rows)
    prompt = f"{load_prompt('table_description')}\nsource: {context}\ncolumns: {header}\nsample rows:\n{rows}"
    if formulas:
        prompt += "\ncalculations used in this table:\n" + "\n".join(formulas)
    data = await call_slm(prompt, _DESCRIPTION_SCHEMA)
    return str(data.get("description", ""))


_STRUCTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "header_rows": {"type": "array", "items": {"type": "integer"}},
                    "col_start": {"type": "integer"},
                    "col_end": {"type": "integer"},
                    "title": {"type": "string"},
                    "notes": {"type": "array", "items": {"type": "string"}},
                    "description": {"type": "string"},
                },
                "required": ["header_rows", "col_start", "col_end", "title", "notes", "description"],
            },
        }
    },
    "required": ["tables"],
}


async def structure_sheet(rows: str, column_hint: str, height: int, width: int) -> list[dict]:
    # one call per sheet: the model is shown only the interesting rows and a body sample (rows, index-tagged) and it
    # returns the table(s) — header rows, column span, title, notes, a description — reasoning over structure it can
    # SEE, never over data it cannot. it decides the semantic calls (what is a header, where a table splits); the
    # mechanical data spans are derived by the caller from the header positions it returns
    prompt = (
        f"{load_prompt('table_structure')}\n"
        f"the sheet has {height} rows (0..{height - 1}) and {width} columns (0..{width - 1}).\n"
        f"column value kinds: {column_hint}\n"
        f"rows:\n{rows}"
    )
    data = await call_slm(prompt, _STRUCTURE_SCHEMA)
    tables = data.get("tables", [])
    return tables if isinstance(tables, list) else []


async def _chat_stream(prompt: str) -> AsyncIterator[str]:
    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": SYNTH_MAX_TOKENS,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    async with (
        slm_slot(),
        httpx.AsyncClient(timeout=SLM_TIMEOUT) as client,
        client.stream("POST", _chat_url(), json=payload) as response,
    ):
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


MIN_SCORE = 1
MAX_SCORE = 3

_SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "score": {"type": "integer"}},
                "required": ["index", "score"],
            },
        }
    },
    "required": ["relevant"],
}


async def select_evidence(query: str, items: list[str]) -> list[tuple[int, int]]:
    if not items:
        return []
    listing = "\n\n".join(f"[{index}] {item}" for index, item in enumerate(items))
    data = await call_slm(f"{load_prompt('evidence_filter')}\nquestion: {query}\nevidence:\n{listing}", _SELECT_SCHEMA)
    out: list[tuple[int, int]] = []
    for entry in data.get("relevant", []):
        if not isinstance(entry, dict):
            continue
        index, score = entry.get("index"), entry.get("score")
        if isinstance(index, int) and 0 <= index < len(items):
            graded = score if isinstance(score, int) else MIN_SCORE
            out.append((index, min(max(graded, MIN_SCORE), MAX_SCORE)))
    return out


_MERGE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


async def merge_evidence(query: str, items: list[str]) -> str:
    listing = "\n\n".join(f"[{index}] {item}" for index, item in enumerate(items))
    data = await call_slm(f"{load_prompt('evidence_merge')}\nquestion: {query}\npassages:\n{listing}", _MERGE_SCHEMA)
    return str(data.get("summary", ""))


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
