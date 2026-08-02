import asyncio
import functools
import json
import logging
import time
from collections.abc import AsyncIterator

from transformers import AutoTokenizer, PreTrainedTokenizerBase

from citadel.prompts import load_prompt
from citadel.schemas.query import QueryPlan
from citadel.services.slm import collect, collect_reply, emit, submit
from config import QWEN_CACHE_DIR, QWEN_HF_REPO, QWEN_MODEL

logger = logging.getLogger(__name__)

STRUCT_MAX_TOKENS = 4096  # structured calls emit short JSON (indices, a concise merge summary, SQL)
STRUCTURE_MAX_TOKENS = 8192  # table structuring emits one entry per real table in the document, plus now up to
# several entries per block when one block is split by row — STRUCT_MAX_TOKENS is sized for a single short object,
# not an array that grows with the document's table count
SYNTH_MAX_TOKENS = 8192  # the streamed answer; the evidence budget reserves this much of the context window for it
SLM_MODEL_LEN = 65536  # qwen --max-model-len: prompt and completion share this one window
# resolution is ONE call over the WHOLE library — splitting it would put the breadth judgment back inside a slice, each
# call weighing the question against a different part of the collection. so this is not a packing budget: it is the
# corpus size past which the design no longer holds, and it is worth saying so before the server says it
RESOLVE_BUDGET = SLM_MODEL_LEN - STRUCT_MAX_TOKENS - 2048


@functools.lru_cache
def get_tokenizer() -> PreTrainedTokenizerBase:
    return AutoTokenizer.from_pretrained(QWEN_HF_REPO, cache_dir=str(QWEN_CACHE_DIR))


def count_tokens(text: str) -> int:
    return len(get_tokenizer().encode(text, add_special_tokens=False))


def count_tokens_batch(texts: list[str]) -> list[int]:
    if not texts:
        return []
    return [len(ids) for ids in get_tokenizer()(texts, add_special_tokens=False)["input_ids"]]


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end != -1 else text


def _struct_payload(prompt: str, schema: dict, max_tokens: int = STRUCT_MAX_TOKENS) -> dict:
    # max_tokens is the HARD completion cap: overrunning it truncates the JSON mid-string and the whole reply fails to
    # parse. a call that asks the model for a long field must raise this above what it asked for, never shrink the ask
    return {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}},
        "chat_template_kwargs": {"enable_thinking": False},
    }


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


async def call_slm(prompt: str, schema: dict, interactive: bool, max_tokens: int = STRUCT_MAX_TOKENS) -> dict:
    raw = await collect(_struct_payload(prompt, _inline_refs(schema), max_tokens), interactive)
    return json.loads(_extract_json(raw))


async def emit_slm(prompt: str, schema: dict, interactive: bool, max_tokens: int = STRUCT_MAX_TOKENS) -> str:
    return await emit(_struct_payload(prompt, _inline_refs(schema), max_tokens), interactive)


async def collect_slm(job_id: str) -> dict:
    return json.loads(_extract_json(await collect_reply(job_id)))


def _text_payload(prompt: str, max_tokens: int) -> dict:
    # streamed so the job runner can time the first token — separating the in-vllm queue+prefill wait from decode. the
    # reply is collected chunk by chunk exactly as before; streaming only changes how the tokens arrive, not the result
    return {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": True,
    }


async def emit_text(prompt: str, max_tokens: int, interactive: bool) -> str:
    # PLAIN TEXT, deliberately. a json schema is for output that is actually structured — indices, sql, a table's shape.
    # a summary is ONE free-text field, and wrapping it in `{"summary": "..."}` only adds an envelope that a length cap
    # can corrupt: hitting max_tokens mid-string leaves unterminated json, which fails to parse and takes the whole
    # batch down with it (measured). asked as text, the same cap merely yields a shorter summary, never a failure
    return await emit(_text_payload(prompt, max_tokens), interactive)


async def collect_text(job_id: str) -> str:
    return (await collect_reply(job_id)).strip()


_STRUCTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "blocks": {"type": "array", "items": {"type": "integer"}},
                    "layout": {"type": "string", "enum": ["relational", "crosstab"]},
                    "transposed": {"type": "boolean"},
                    "header_rows": {"type": "array", "items": {"type": "integer"}},
                    "row_end": {"type": "integer"},
                    "col_start": {"type": "integer"},
                    "col_end": {"type": "integer"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "section_rows": {"type": "array", "items": {"type": "integer"}},
                    "crosstab": {
                        "type": "object",
                        "properties": {
                            "key_columns": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}, "col": {"type": "integer"}},
                                    "required": ["name", "col"],
                                },
                            },
                            "dimensions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}, "header_row": {"type": "integer"}},
                                    "required": ["name", "header_row"],
                                },
                            },
                            "value_name": {"type": "string"},
                            "value_col_start": {"type": "integer"},
                            "value_col_end": {"type": "integer"},
                        },
                        "required": ["key_columns", "dimensions", "value_name", "value_col_start", "value_col_end"],
                    },
                    "title": {"type": "string"},
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "blocks",
                    "layout",
                    "transposed",
                    "header_rows",
                    "row_end",
                    "col_start",
                    "col_end",
                    "columns",
                    "section_rows",
                    "title",
                    "notes",
                ],
            },
        }
    },
    "required": ["tables"],
}


def _structure_tables(data: dict) -> list[dict]:
    tables = data.get("tables", [])
    return tables if isinstance(tables, list) else []


async def emit_structure_candidates(payload: str) -> str:
    # the unified stage: `payload` lays out every block for one document, each labelled by number. the model returns the
    # real tables — each naming its source block(s) so a split table's fragments come back as one — with structure
    prompt = f"{load_prompt('table_structure')}\n{payload}"
    return await emit_slm(prompt, _STRUCTURE_SCHEMA, interactive=False, max_tokens=STRUCTURE_MAX_TOKENS)


async def collect_structure_candidates(job_id: str) -> list[dict]:
    return _structure_tables(await collect_slm(job_id))


async def _chat_stream(prompt: str) -> AsyncIterator[str]:
    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": SYNTH_MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": True,
    }
    async for delta in submit(payload, interactive=True):
        yield delta


_MERGE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


async def merge_evidence(query: str, items: list[str]) -> str:
    listing = "\n\n".join(f"[{index}] {item}" for index, item in enumerate(items))
    data = await call_slm(
        f"{load_prompt('evidence_merge')}\nquestion: {query}\npassages:\n{listing}", _MERGE_SCHEMA, interactive=True
    )
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

# deepest first: an item named twice within its own kind is taken at the deepest naming. tables have no `parts` — a
# table is either known by what it is (metadata) or read for its values (data), never a document-shaped middle rung
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
    # exposed so a caller can size a batch of items BEFORE calling emit_resolve — the same accounting that call itself
    # uses, so a batch built to fit this is guaranteed to fit the real call
    return count_tokens(_resolve_prompt(query, items, library))


async def emit_resolve(query: str, items: list[str], library: str = "") -> str:
    # one grain — every document by its summary, every table by its identity and columns — read against the question
    # in ONE call. what comes back is which items the answer must account for and how deeply each has to be read;
    # everything downstream executes that, and nothing downstream decides membership again. `items` here is whatever
    # the caller packed into one call — the whole library when it fits, one batch of it otherwise (see query.resolve).
    # split emit/collect, exactly like validate_tables below: every batch's job is queued before any reply is awaited,
    # so batches sit on the shared stream together rather than one at a time behind the caller's own await
    prompt = _resolve_prompt(query, items, library)
    tokens = count_tokens(prompt)
    if tokens > RESOLVE_BUDGET:
        logger.warning("resolve inventory does not fit one call tokens=%d budget=%d", tokens, RESOLVE_BUDGET)
    return await emit_slm(prompt, _RESOLVE_SCHEMA, interactive=True)


async def collect_resolve(job_id: str, count: int) -> tuple[dict[int, str], dict[int, str]]:
    data = await collect_slm(job_id)
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


_VALIDATE_SCHEMA = {
    "type": "object",
    "properties": {"tables": {"type": "array", "items": {"type": "integer"}}},
    "required": ["tables"],
}


def _pack_indices(counts: list[int], budget: int) -> list[list[int]]:
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


def _validate_prompt(candidates: list[str]) -> str:
    listing = "\n\n".join(f"[{index}] {candidate}" for index, candidate in enumerate(candidates))
    return f"{load_prompt('table_validation')}\ncandidates:\n{listing}"


async def validate_tables(candidates: list[str]) -> list[bool]:
    # which of a document's candidate tables are real. per-candidate judgment, so an oversized candidate set splits
    # across concurrent batches losslessly — each batch decides its own members, nothing needs cross-candidate context
    if not candidates:
        return []
    counts = await asyncio.to_thread(count_tokens_batch, candidates)
    packs = _pack_indices(counts, RESOLVE_BUDGET)
    jobs: list[tuple[list[int], str]] = []
    for pack in packs:
        job_id = await emit_slm(_validate_prompt([candidates[i] for i in pack]), _VALIDATE_SCHEMA, False)
        jobs.append((pack, job_id))
    keep = [False] * len(candidates)
    for pack, job_id in jobs:
        data = await collect_slm(job_id)
        for local in data.get("tables", []):
            if isinstance(local, int) and 0 <= local < len(pack):
                keep[pack[local]] = True
    logger.info("validate_tables candidates=%d packs=%d kept=%d", len(candidates), len(packs), sum(keep))
    return keep


async def write_queries(query: str, tables: list[str], library: str = "") -> QueryPlan:
    if not tables:
        return QueryPlan()
    listing = "\n\n".join(tables)
    prompt = f"{load_prompt('text_to_sql')}\nlibrary: {library}\nquestion: {query}\ntables:\n{listing}"
    started = time.time()
    data = await call_slm(prompt, _QUERIES_SCHEMA, interactive=True)
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
    if not queries:  # every table here was resolved as needing values from its rows, so nothing asked is a real miss
        logger.warning("write_queries returned nothing raw=%s", data)
    return plan


async def synthesize(query: str, passages: list[str], results: list[str]) -> AsyncIterator[str]:
    evidence = "passages:\n" + "\n".join(passages) + "\n\ntable results:\n" + "\n".join(results)
    async for token in _chat_stream(f"{load_prompt('synthesize')}\nquestion: {query}\n{evidence}"):
        yield token
