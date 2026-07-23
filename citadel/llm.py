import functools
import json
import logging
from collections.abc import AsyncIterator

from transformers import AutoTokenizer, PreTrainedTokenizerBase

from citadel.prompts import load_prompt
from citadel.services.slm import collect, collect_reply, emit, submit
from config import QWEN_CACHE_DIR, QWEN_HF_REPO, QWEN_MODEL

logger = logging.getLogger(__name__)

STRUCT_MAX_TOKENS = 4096  # structured calls emit short JSON (indices, a concise merge summary, SQL)
SYNTH_MAX_TOKENS = 8192  # the streamed answer; the evidence budget reserves this much of the context window for it
SLM_MODEL_LEN = 65536  # qwen --max-model-len: prompt and completion share this one window


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


def _struct_payload(prompt: str, schema: dict) -> dict:
    return {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": STRUCT_MAX_TOKENS,
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


async def call_slm(prompt: str, schema: dict, interactive: bool) -> dict:
    raw = await collect(_struct_payload(prompt, _inline_refs(schema)), interactive)
    return json.loads(_extract_json(raw))


async def emit_slm(prompt: str, schema: dict, interactive: bool) -> str:
    return await emit(_struct_payload(prompt, _inline_refs(schema)), interactive)


async def collect_slm(job_id: str) -> dict:
    return json.loads(_extract_json(await collect_reply(job_id)))


_STRUCTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "layout": {"type": "string", "enum": ["relational", "crosstab"]},
                    "header_rows": {"type": "array", "items": {"type": "integer"}},
                    "col_start": {"type": "integer"},
                    "col_end": {"type": "integer"},
                    "columns": {"type": "array", "items": {"type": "string"}},
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
                "required": ["layout", "header_rows", "col_start", "col_end", "columns", "title", "notes"],
            },
        }
    },
    "required": ["tables"],
}


async def structure_sheet(rows: str, column_hint: str, height: int, width: int) -> list[dict]:
    # one call per sheet: the model is shown only the interesting rows and a body sample (rows, index-tagged) and it
    # returns the table(s) — header rows, column span, title, notes — reasoning over structure it can
    # SEE, never over data it cannot. it decides the semantic calls (what is a header, where a table splits); the
    # mechanical data spans are derived by the caller from the header positions it returns
    prompt = (
        f"{load_prompt('table_structure')}\n"
        f"the sheet has {height} rows (0..{height - 1}) and {width} columns (0..{width - 1}).\n"
        f"column value kinds: {column_hint}\n"
        f"rows:\n{rows}"
    )
    data = await call_slm(prompt, _STRUCTURE_SCHEMA, interactive=False)
    tables = data.get("tables", [])
    return tables if isinstance(tables, list) else []


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
    data = await call_slm(
        f"{load_prompt('evidence_filter')}\nquestion: {query}\nevidence:\n{listing}", _SELECT_SCHEMA, interactive=True
    )
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
    data = await call_slm(
        f"{load_prompt('evidence_merge')}\nquestion: {query}\npassages:\n{listing}", _MERGE_SCHEMA, interactive=True
    )
    return str(data.get("summary", ""))


_QUERIES_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
}


_BATCH_SELECT_SCHEMA = {
    "type": "object",
    "properties": {"sections": {"type": "array", "items": {"type": "integer"}}},
    "required": ["sections"],
}


def _select_prompt(query: str, items: list[str]) -> str:
    listing = "\n\n".join(f"[{index}] {item}" for index, item in enumerate(items))
    return f"{load_prompt('batch_select')}\nquestion: {query}\nsections:\n{listing}"


def _select_indices(data: dict, count: int) -> list[int]:
    seen: list[int] = []
    for index in data.get("sections", []):
        if isinstance(index, int) and 0 <= index < count and index not in seen:
            seen.append(index)
    return seen


async def emit_select(query: str, items: list[str]) -> str:
    return await emit_slm(_select_prompt(query, items), _BATCH_SELECT_SCHEMA, interactive=True)


async def collect_select(job_id: str, count: int) -> list[int]:
    return _select_indices(await collect_slm(job_id), count)


async def write_queries(query: str, tables: list[str]) -> list[str]:
    if not tables:
        return []
    listing = "\n\n".join(tables)
    prompt = f"{load_prompt('text_to_sql')}\nquestion: {query}\ntables:\n{listing}"
    data = await call_slm(prompt, _QUERIES_SCHEMA, interactive=True)
    queries = [str(sql) for sql in data.get("queries", []) if str(sql).strip()]
    logger.info("write_queries tables=%d prompt_tokens=%d queries=%d", len(tables), count_tokens(prompt), len(queries))
    for query in queries:
        logger.info("  sql: %s", query)
    if not queries:  # zero SQL on a tabular question is a failure worth seeing the raw answer for
        logger.warning("write_queries returned nothing raw=%s", data)
    return queries


async def synthesize(query: str, passages: list[str], results: list[str]) -> AsyncIterator[str]:
    evidence = "passages:\n" + "\n".join(passages) + "\n\ntable results:\n" + "\n".join(results)
    async for token in _chat_stream(f"{load_prompt('synthesize')}\nquestion: {query}\n{evidence}"):
        yield token
