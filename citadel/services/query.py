import asyncio
import itertools
import json
import logging
import re
import time
import traceback
from collections.abc import AsyncIterator
from dataclasses import dataclass
from itertools import starmap
from typing import Any
from typing import cast as type_cast

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Numeric, Select, Text, case, cast, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import TypeEngine

from citadel.bus import get_redis
from citadel.db import get_sessionmaker
from citadel.llm import (
    RESOLVE_BUDGET,
    SLM_MODEL_LEN,
    STRUCT_MAX_TOKENS,
    SYNTH_MAX_TOKENS,
    collect_resolve,
    count_tokens,
    count_tokens_batch,
    emit_resolve,
    merge_evidence,
    resolve_prompt_tokens,
    synthesize,
    write_queries,
)
from citadel.models.table import TableRow
from citadel.schemas.query import QueryPlan
from citadel.services.capacity import get_text_large_capacity
from citadel.services.retrieval import (
    BatchRef,
    DocRef,
    TableCand,
    load_all_tables,
    load_block_texts,
    load_library_batches,
    load_library_documents,
    load_library_name,
    scope_block_ids,
)
from config import get_settings

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 3000
_call_no = itertools.count()


def _capacity_key(label: str) -> str:
    return f"{get_settings().worker_id}:{label}:{next(_call_no)}"


SYNTH_BUDGET = SLM_MODEL_LEN - SYNTH_MAX_TOKENS - 2048
MERGE_INPUT_BUDGET = STRUCT_MAX_TOKENS // 2
SCHEMA_SAMPLES = 3
_PG = postgresql.dialect()
_DTYPE_SA: dict[str, type[TypeEngine[Any]]] = {
    "integer": BigInteger,
    "float": DOUBLE_PRECISION,
    "decimal": Numeric,
    "boolean": Boolean,
    "date": Date,
    "datetime": DateTime,
    "string": Text,
}
_TABLE_REF = re.compile(r"\bt(\d+)\b")
_COL_REF = re.compile(r"\bt(\d+)\.c(\d+)\b")
_INT_RE = r"^[[:space:]]*[-+]?[[:digit:]]+[[:space:]]*$"
_FLOAT_RE = r"^[[:space:]]*[-+]?([[:digit:]]+[.]?[[:digit:]]*|[.][[:digit:]]+)([eE][-+]?[[:digit:]]+)?[[:space:]]*$"
_BOOL_RE = r"^[[:space:]]*(true|false|t|f|yes|no|y|n|0|1)[[:space:]]*$"
_CELL_SENTINELS = ("", "NULL", "null", "Null", "NaN", "nan", "N/A", "n/a", "None", "none", "-", "—")


def _typed_column(index: int, dtype: str) -> Any:
    raw = TableRow.values.op("->>")(index)
    label = f"c{index}"
    sa_type = _DTYPE_SA.get(dtype, Text)
    if sa_type is Text:
        return raw.label(label)
    if sa_type is BigInteger:
        return case((raw.op("~")(_INT_RE), cast(raw, sa_type)), else_=None).label(label)
    if sa_type in {DOUBLE_PRECISION, Numeric}:
        return case((raw.op("~")(_FLOAT_RE), cast(raw, sa_type)), else_=None).label(label)
    if sa_type is Boolean:
        return case((raw.op("~*")(_BOOL_RE), cast(raw, sa_type)), else_=None).label(label)
    cleaned: Any = raw
    for sentinel in _CELL_SENTINELS:
        cleaned = func.nullif(cleaned, sentinel)
    return cast(cleaned, sa_type).label(label)


@dataclass
class SqlResult:
    label: str
    columns: list[str]
    rows: list[list]
    total: int
    refs: list[int]
    query: str


def _view_select(table: TableCand) -> Select[Any]:
    columns = [_typed_column(index, column.get("dtype", "string")) for index, column in enumerate(table.columns)]
    stmt = select(*columns).where(TableRow.table_id == table.table_id)
    if table.header_rows:
        stmt = stmt.where(TableRow.row_idx.notin_(table.header_rows))
    return stmt


def _view_cte(index: int, table: TableCand) -> str:
    compiled = _view_select(table).compile(dialect=_PG, compile_kwargs={"literal_binds": True})
    return "t" + str(index) + " AS (" + str(compiled) + ")"


def _samples(sample_rows: list[list], index: int) -> str:
    values = [row[index] for row in sample_rows[:SCHEMA_SAMPLES] if index < len(row) and row[index] is not None]
    return f"  e.g. {', '.join(str(value) for value in values)}" if values else ""


def _schema(columns: list[dict], sample_rows: list[list]) -> str:
    lines: list[str] = []
    for index, column in enumerate(columns):
        label = f"c{index}: {column.get('header') or '?'} ({column.get('dtype', 'string')})"
        lines.append(label + _samples(sample_rows, index))
    return "\n".join(lines)


def _schema_block(index: int, table: TableCand, labels: dict[int, str]) -> str:
    return f"t{index} ({_table_label(table, labels)}) rows={table.n_rows}\n{_schema(table.columns, table.sample_rows)}"


def _table_locator(table: TableCand) -> str:
    sheet = table.metadata.get("sheet")
    if sheet:
        return str(sheet)
    return f"p{table.page_no}" if table.page_no is not None else ""


def _base_table_label(table: TableCand) -> str:
    locator = _table_locator(table)
    return f"{table.filename} ({locator})" if locator else table.filename


def _table_labels(tables: list[TableCand]) -> dict[int, str]:
    groups: dict[str, list[TableCand]] = {}
    for table in tables:
        groups.setdefault(_base_table_label(table), []).append(table)
    labels: dict[int, str] = {}
    for base, group in groups.items():
        if len(group) == 1:
            labels[group[0].table_id] = base
            continue
        for ordinal, table in enumerate(sorted(group, key=lambda t: t.table_id), start=1):
            labels[table.table_id] = f"{base} #{ordinal}"
    return labels


def _table_label(table: TableCand, labels: dict[int, str]) -> str:
    return labels[table.table_id]


def _table_render(table: TableCand, labels: dict[int, str]) -> str:
    meta = table.metadata
    described = [str(meta[key]) for key in ("title", "caption") if meta.get(key)]
    described.extend(str(note) for note in meta.get("notes") or [])
    head = f"[{_table_label(table, labels)}] rows={table.n_rows}"
    return f"{head} {' | '.join(described)}" if described else head


def _cte(tables: list[TableCand]) -> str:
    return "WITH " + ", ".join(starmap(_view_cte, enumerate(tables)))


def _refs(tables: list[TableCand], sql: str) -> list[int]:
    return sorted({index for match in _TABLE_REF.findall(sql) if (index := int(match)) < len(tables)})


def _col_name(tables: list[TableCand], table: int, col: int) -> str:
    if table < len(tables) and col < len(tables[table].columns):
        return str(tables[table].columns[col].get("header") or f"c{col}")
    return f"c{col}"


def _readable_sql(tables: list[TableCand], sql: str, labels: dict[int, str]) -> str:
    sql = _COL_REF.sub(lambda m: _col_name(tables, int(m[1]), int(m[2])), sql)
    return _TABLE_REF.sub(lambda m: _table_label(tables[int(m[1])], labels) if int(m[1]) < len(tables) else m[0], sql)


def _sources(tables: list[TableCand], refs: list[int], labels: dict[int, str]) -> list[str]:
    return list(dict.fromkeys(_table_label(tables[index], labels) for index in refs))


def _row_file_sources(columns: list[str], rows: list[list]) -> list[str]:
    if "file" not in columns:
        return []
    file_at = columns.index("file")
    sheet_at = columns.index("sheet") if "sheet" in columns else None
    labels: list[str] = []
    for row in rows:
        name = row[file_at]
        if name is None:
            continue
        sheet = str(row[sheet_at]) if sheet_at is not None and row[sheet_at] else ""
        labels.append(f"{name} ({sheet})" if sheet else str(name))
    return list(dict.fromkeys(labels))


def _all_files(tables: list[TableCand]) -> list[str]:
    return list(dict.fromkeys(table.filename for table in tables))


def _result_sources(
    tables: list[TableCand], refs: list[int], columns: list[str], rows: list[list], labels: dict[int, str]
) -> list[str]:
    if refs:
        return _sources(tables, refs, labels)
    return _row_file_sources(columns, rows) or _all_files(tables)


async def _run_sql(session: AsyncSession, tables: list[TableCand], sql: str) -> tuple[list[str], list[list]]:
    cte = _cte(tables)
    await session.execute(text("SET TRANSACTION READ ONLY"))
    await session.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(STATEMENT_TIMEOUT_MS)})
    connection = await session.connection()
    result = await connection.exec_driver_sql(f"{cte} {sql}")
    return list(result.keys()), [list(row) for row in result.fetchall()]


async def _execute(tables: list[TableCand], sql: str, labels: dict[int, str]) -> SqlResult | None:
    sql = sql.strip().rstrip(";").strip()
    try:
        async with get_sessionmaker()() as session, session.begin():
            columns, rows = await _run_sql(session, tables, sql)
    except SQLAlchemyError:
        logger.warning("sql failed sql=%s\n%s", sql, traceback.format_exc())
        return None
    refs = _refs(tables, sql)
    sources = _result_sources(tables, refs, columns, rows, labels)
    logger.info("resolve sources=%s rows=%d sql=%s", sources, len(rows), sql)
    return SqlResult(
        ", ".join(sources) or "computed result", columns, rows, len(rows), refs, _readable_sql(tables, sql, labels)
    )


async def _plan_queries(question: str, blocks: list[str], library: str) -> QueryPlan:
    key = _capacity_key("queries")
    cap = get_text_large_capacity()
    await cap.acquire(key)
    try:
        return await write_queries(question, blocks, library)
    finally:
        await cap.release(key)


async def _aggregate(
    question: str, tables: list[TableCand], labels: dict[int, str], library: str = ""
) -> list[SqlResult]:
    blocks = [_schema_block(index, table, labels) for index, table in enumerate(tables)]
    plan = await _plan_queries(question, blocks, library) if tables else QueryPlan()
    results: list[SqlResult] = []
    for sql in plan.queries:
        resolved = await _execute(tables, sql, labels)
        if resolved is not None:
            results.append(resolved)
    logger.info("aggregate tables=%d sqls=%d ran=%d", len(tables), len(plan.queries), len(results))
    return results


def _row_text(row: list) -> str:
    return " | ".join("" if value is None else str(value) for value in row)


def _result_render(result: SqlResult) -> str:
    shown = len(result.rows)
    head = f"[{result.label}]" if shown >= result.total else f"[{result.label}] showing {shown} of {result.total} rows"
    return "\n".join(
        [head, f"computed by: {result.query}", " | ".join(result.columns), *(_row_text(row) for row in result.rows)]
    )


def _fit_results(results: list[SqlResult], budget: int) -> list[SqlResult]:
    if not results or budget <= 0:
        return []
    kept: list[list[list]] = [[] for _ in results]
    used = sum(
        count_tokens(f"[{result.label}]\ncomputed by: {result.query}\n" + " | ".join(result.columns))
        for result in results
    )
    pointer = [0] * len(results)
    added = True
    while added:
        added = False
        for index, result in enumerate(results):
            cursor = pointer[index]
            if cursor >= len(result.rows):
                continue
            cost = count_tokens(_row_text(result.rows[cursor])) + 1
            if used + cost > budget:
                continue
            kept[index].append(result.rows[cursor])
            used += cost
            pointer[index] += 1
            added = True
    return [SqlResult(r.label, r.columns, kept[i], r.total, r.refs, r.query) for i, r in enumerate(results) if kept[i]]


@dataclass
class _TableChannel:
    blocks: list[str]
    fitted: list[SqlResult]
    described: int


def _table_channel(results: list[SqlResult], describe: list[str], rows_budget: int) -> _TableChannel:
    fitted = _fit_results(results, rows_budget)
    return _TableChannel([*describe, *(_result_render(result) for result in fitted)], fitted, len(describe))


@dataclass
class _Evidence:
    text: str
    sources: list[int]
    tokens: int
    score: int
    heading: str | None
    document_id: int


def _group_key(evidence: _Evidence, level: int) -> object:
    if level == 0:
        return (evidence.document_id, evidence.heading)
    if level == 1:
        return evidence.document_id
    return 0


def _chunk_by_tokens(items: list[_Evidence], budget: int) -> list[list[_Evidence]]:
    chunks: list[list[_Evidence]] = []
    current: list[_Evidence] = []
    used = 0
    for item in items:
        if current and used + item.tokens > budget:
            chunks.append(current)
            current, used = [], 0
        current.append(item)
        used += item.tokens
    if current:
        chunks.append(current)
    return chunks


async def _merge_chunk(question: str, chunk: list[_Evidence]) -> _Evidence:
    key = _capacity_key("merge")
    cap = get_text_large_capacity()
    await cap.acquire(key)
    try:
        summary = await merge_evidence(question, [evidence.text for evidence in chunk])
    finally:
        await cap.release(key)
    sources = [source for evidence in chunk for source in evidence.sources]
    if summary:
        tokens = count_tokens(summary)
    else:
        summary = "\n".join(evidence.text for evidence in chunk)
        tokens = sum(evidence.tokens for evidence in chunk)
    return _Evidence(summary, sources, tokens, max(e.score for e in chunk), chunk[0].heading, chunk[0].document_id)


async def _reduce(question: str, evidences: list[_Evidence], budget: int, level: int) -> list[_Evidence]:
    total = sum(evidence.tokens for evidence in evidences)
    if total <= budget:
        return evidences
    overflow = total - budget
    order = sorted(range(len(evidences)), key=lambda index: (evidences[index].score, -evidences[index].tokens))
    marked: set[int] = set()
    marked_tokens = 0
    for index in order:
        marked.add(index)
        marked_tokens += evidences[index].tokens
        if marked_tokens >= overflow:
            break
    groups: dict[object, list[_Evidence]] = {}
    for index in marked:
        groups.setdefault(_group_key(evidences[index], level), []).append(evidences[index])
    chunks = [chunk for items in groups.values() for chunk in _chunk_by_tokens(items, MERGE_INPUT_BUDGET)]
    merged = await asyncio.gather(*(_merge_chunk(question, chunk) for chunk in chunks))
    survivors = [evidences[index] for index in range(len(evidences)) if index not in marked]
    combined = survivors + list(merged)
    if sum(evidence.tokens for evidence in combined) >= total:
        return combined
    return await _reduce(question, combined, budget, level + 1)


def _doc_item(doc: DocRef) -> str:
    return f"document — {doc.filename}: {doc.summary}"


def _table_item(table: TableCand, labels: dict[int, str]) -> str:
    named = [str(table.metadata[key]) for key in ("title", "caption") if table.metadata.get(key)]
    columns = ", ".join(str(column.get("header") or "?") for column in table.columns)
    head = f"table — {_table_label(table, labels)} rows={table.n_rows}"
    return f"{head}: {' | '.join(named)}. columns {columns}" if named else f"{head}: columns {columns}"


@dataclass
class _Coverage:
    documents: dict[int, str]
    tables: dict[int, str]


def _inventory(
    documents: list[DocRef], tables: list[TableCand], labels: dict[int, str]
) -> tuple[list[str], list[tuple[str, int]]]:
    by_document: dict[int, list[int]] = {}
    for index, table in enumerate(tables):
        by_document.setdefault(table.document_id, []).append(index)
    items: list[str] = []
    index_map: list[tuple[str, int]] = []
    for doc_index, doc in enumerate(documents):
        items.append(_doc_item(doc))
        index_map.append(("doc", doc_index))
        for table_index in by_document.get(doc.id, []):
            items.append(_table_item(tables[table_index], labels))
            index_map.append(("table", table_index))
    return items, index_map


def _split_coverage(
    doc_coverage: dict[int, str], table_coverage: dict[int, str], index_map: list[tuple[str, int]]
) -> _Coverage:
    documents: dict[int, str] = {}
    tables: dict[int, str] = {}
    for index, depth in doc_coverage.items():
        if index >= len(index_map):
            continue
        kind, orig = index_map[index]
        if kind != "doc":
            logger.warning("resolve named a table as a document item=%d depth=%s — dropped", index, depth)
            continue
        documents[orig] = depth
    for index, depth in table_coverage.items():
        if index >= len(index_map):
            continue
        kind, orig = index_map[index]
        if kind != "table":
            logger.warning("resolve named a document as a table item=%d depth=%s — dropped", index, depth)
            continue
        tables[orig] = depth
    return _Coverage(documents, tables)


def _pack_documents(
    documents: list[DocRef], tables: list[TableCand], labels: dict[int, str], question: str, library: str
) -> list[list[DocRef]]:
    batches: list[list[DocRef]] = []
    current: list[DocRef] = []
    for doc in documents:
        candidate = [*current, doc]
        items, _ = _inventory(candidate, tables, labels)
        tokens = resolve_prompt_tokens(question, items, library)
        if current and tokens > RESOLVE_BUDGET:
            batches.append(current)
            current = [doc]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


STREAM_RESOLVE_BATCH = "resolve_batch"


def _resolve_pending_key(library_id: int) -> str:
    return f"resolve:pending:{library_id}"


def _resolve_results_key(library_id: int) -> str:
    return f"resolve:results:{library_id}"


def _resolve_done_stream(library_id: int) -> str:
    return f"resolve:done:{library_id}"


async def emit_resolve_batches(
    library_id: int,
    question: str,
    library: str,
    batches: list[list[DocRef]],
    tables: list[TableCand],
    labels: dict[int, str],
) -> int:
    redis = get_redis()
    await redis.delete(_resolve_done_stream(library_id))
    await redis.delete(_resolve_results_key(library_id))
    jobs: list[tuple[int, list[str], list[tuple[str, int]]]] = []
    for batch_no, batch in enumerate(batches):
        items, index_map = _inventory(batch, tables, labels)
        if items:
            jobs.append((batch_no, items, index_map))
    await redis.set(_resolve_pending_key(library_id), len(jobs))
    for batch_no, items, index_map in jobs:
        await redis.xadd(
            STREAM_RESOLVE_BATCH,
            {
                "library_id": str(library_id),
                "batch_no": str(batch_no),
                "question": question,
                "library": library,
                "items": json.dumps(items),
                "index_map": json.dumps(index_map),
            },
        )
    return len(jobs)


async def resolve_batch_job(fields: dict[str, str]) -> None:
    library_id = int(fields["library_id"])
    items = json.loads(fields["items"])
    index_map = [(kind, index) for kind, index in json.loads(fields["index_map"])]
    key = f"resolve:{library_id}:{fields['batch_no']}"
    cap = get_text_large_capacity()
    await cap.acquire(key)
    try:
        job_id = await emit_resolve(fields["question"], items, key, fields["library"])
        doc_coverage, table_coverage = await collect_resolve(job_id, len(items))
    finally:
        await cap.release(key)
    coverage = _split_coverage(doc_coverage, table_coverage, index_map)
    payload = json.dumps({"documents": coverage.documents, "tables": coverage.tables})
    await get_redis().hset(_resolve_results_key(library_id), fields["batch_no"], payload)


async def record_resolve_batch(library_id: int) -> None:
    redis = get_redis()
    remaining = await redis.decr(_resolve_pending_key(library_id))
    if remaining != 0:
        return
    raw_results = await redis.hgetall(_resolve_results_key(library_id))
    merged_documents: dict[int, str] = {}
    merged_tables: dict[int, str] = {}
    for raw in raw_results.values():
        parsed = json.loads(raw)
        merged_documents.update({int(index): depth for index, depth in parsed["documents"].items()})
        merged_tables.update({int(index): depth for index, depth in parsed["tables"].items()})
    await redis.xadd(
        _resolve_done_stream(library_id),
        {"documents": json.dumps(merged_documents), "tables": json.dumps(merged_tables)},
    )


async def wait_resolve(library_id: int, dispatched: int) -> _Coverage:
    if dispatched == 0:
        return _Coverage({}, {})
    entries = type_cast(
        "list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]",
        await get_redis().xread({_resolve_done_stream(library_id): "0"}, block=0),
    )
    _, messages = entries[0]
    _, raw = messages[0]
    documents = {int(index): depth for index, depth in json.loads(raw[b"documents"]).items()}
    tables = {int(index): depth for index, depth in json.loads(raw[b"tables"]).items()}
    return _Coverage(documents, tables)


async def resolve(
    library_id: int,
    question: str,
    documents: list[DocRef],
    tables: list[TableCand],
    labels: dict[int, str],
    library: str,
) -> _Coverage:
    batches = _pack_documents(documents, tables, labels, question, library)
    logger.info("resolve batches=%d documents=%d tables=%d", len(batches), len(documents), len(tables))
    dispatched = await emit_resolve_batches(library_id, question, library, batches, tables, labels)
    return await wait_resolve(library_id, dispatched)


async def run_tables(
    question: str, tables: list[TableCand], depths: dict[int, str], labels: dict[int, str], library: str
) -> list[SqlResult]:
    queried = [tables[index] for index in sorted(depths) if depths[index] == "data"]
    if not queried:
        return []
    return await _aggregate(question, queried, labels, library)


def _batch_cite(batch: BatchRef) -> str:
    if batch.start_page_no is None:
        return batch.filename
    if batch.end_page_no and batch.end_page_no != batch.start_page_no:
        return f"{batch.filename} p{batch.start_page_no}-p{batch.end_page_no}"
    return f"{batch.filename} p{batch.start_page_no}"


def _summary_text(batch: BatchRef) -> str:
    return f"[{_batch_cite(batch)}] {batch.summary}"


def _doc_summary_text(doc: DocRef) -> str:
    return f"[{doc.filename}] {doc.summary}"


def _doc_evidence(doc: DocRef, tokens: int) -> _Evidence:
    return _Evidence(_doc_summary_text(doc), [doc.id], tokens, 0, None, doc.id)


async def _reduce_floor(question: str, documents: list[DocRef], counts: list[int], budget: int) -> list[str]:
    evidences = [_doc_evidence(doc, counts[index]) for index, doc in enumerate(documents)]
    reduced = await _reduce(question, evidences, budget, 0)
    logger.info("text floor reduced documents=%d out=%d budget=%d", len(documents), len(reduced), budget)
    return [evidence.text for evidence in reduced]


async def _doc_blocks(batches: list[BatchRef]) -> list[str]:
    ranges = [(batch.document_id, batch.start_block_ordinal, batch.end_block_ordinal) for batch in batches]
    texts = await load_block_texts(await scope_block_ids(ranges))
    blocks = sorted(texts.values(), key=lambda block: (block.document_id, block.block_ordinal))
    return [block.text for block in blocks]


async def _deeper_texts(depth: str, batches: list[BatchRef]) -> list[str]:
    if depth == "parts":
        return [_summary_text(batch) for batch in batches]
    return await _doc_blocks(batches)


def _upgrade_order(documents: list[DocRef], depths: dict[int, str]) -> list[tuple[DocRef, tuple[str, ...], bool]]:
    ladders = {"wording": ("wording", "parts"), "parts": ("parts",)}
    asked: list[tuple[DocRef, tuple[str, ...], bool]] = [
        (doc, ladders[depth], False)
        for depth in ("wording", "parts")
        for doc in documents
        if depths.get(doc.id) == depth
    ]
    return asked + [(doc, ("parts",), True) for doc in documents if depths.get(doc.id, "overview") == "overview"]


async def _upgrade(
    doc: DocRef, ladder: tuple[str, ...], batches: list[BatchRef], floor_cost: int, allowance: int
) -> tuple[list[str], int, str] | None:
    for depth in ladder:
        stored = doc.content_tokens if depth == "wording" else sum(batch.summary_tokens for batch in batches)
        if stored > allowance + floor_cost:
            continue
        texts = await _deeper_texts(depth, batches)
        if not texts:
            continue
        cost = sum(await asyncio.to_thread(count_tokens_batch, texts))
        if cost - floor_cost <= allowance:
            return texts, cost - floor_cost, depth
    return None


async def render_text(
    question: str, documents: list[DocRef], depths: dict[int, str], batches: dict[int, list[BatchRef]], budget: int
) -> list[str]:
    if not documents or budget <= 0:
        logger.info("text floor documents=%d budget=%d", len(documents), budget)
        return []
    floor = [_doc_summary_text(doc) for doc in documents]
    counts = await asyncio.to_thread(count_tokens_batch, floor)
    if sum(counts) > budget:
        return await _reduce_floor(question, documents, counts, budget)
    spare = budget - sum(counts)
    share = spare // len(documents)
    rendered = {doc.id: [text] for doc, text in zip(documents, floor, strict=True)}
    costs = dict(zip((doc.id for doc in documents), counts, strict=True))
    deepened: dict[int, str] = {}
    greedy: list[str] = []
    for doc, ladder, spent_spare in _upgrade_order(documents, depths):
        upgraded = await _upgrade(doc, ladder, batches.get(doc.id, []), costs[doc.id], min(share, spare))
        if upgraded is not None:
            rendered[doc.id], delta, reached = upgraded
            spare -= delta
            deepened[doc.id] = reached
            if spent_spare:
                greedy.append(doc.filename)
    logger.info(
        "text documents=%d floor=%d share=%d asked=%s greedy=%d capped=%s spare=%d budget=%d",
        len(documents),
        sum(counts),
        share,
        {doc.filename: deepened[doc.id] for doc in documents if doc.id in deepened and doc.filename not in greedy},
        len(greedy),
        [doc.filename for doc in documents if doc.id not in deepened],
        spare,
        budget,
    )
    return [text for doc in documents for text in rendered[doc.id]]


_CITE = re.compile(r"\[([^\]]+)\]")


def _cite_file(label: str) -> str:
    return re.sub(r"\s+p\d.*$", "", re.sub(r"\s*\([^)]*\)\s*$", "", label)).strip()


def _evidence_files(passages: list[str]) -> set[str]:
    files: set[str] = set()
    for passage in passages:
        match = _CITE.match(passage)
        if match:
            files.add(_cite_file(match.group(1)))
    return files


def _result_fallback_files(results: list[SqlResult], table_labels: set[str]) -> set[str]:
    return {_cite_file(part) for result in results for part in result.label.split(", ") if part not in table_labels}


def _check_citations(response: str, evidence_files: set[str], table_labels: set[str]) -> None:
    cited = set(_CITE.findall(response))
    fabricated = sorted(
        label for label in cited if label not in table_labels and _cite_file(label) not in evidence_files
    )
    if fabricated:
        logger.error(
            "HALLUCINATION cited sources not in evidence=%s | files=%s | tables=%s",
            fabricated,
            sorted(evidence_files),
            sorted(table_labels),
        )
    logger.info("answer chars=%d cited_files=%d fabricated=%d", len(response), len(cited), len(fabricated))
    logger.debug("answer:\n%s", response)


@dataclass
class _Assembled:
    passages: list[str]
    rendered: list[str]
    channel: _TableChannel


def _log_evidence(
    question: str,
    started: float,
    passages: list[str],
    results: list[SqlResult],
    channel: _TableChannel,
) -> None:
    table_tokens = sum(count_tokens(block) for block in channel.blocks)
    logger.info(
        "query done %r results=%d/%d described=%d table_tokens=%d dropped_rows=%d passages=%d synth_tokens=%d/%d %.1fs",
        question[:80],
        len(channel.fitted),
        len(results),
        channel.described,
        table_tokens,
        sum(r.total for r in results) - sum(len(r.rows) for r in channel.fitted),
        len(passages),
        table_tokens + sum(count_tokens(block) for block in passages),
        SYNTH_BUDGET,
        time.time() - started,
    )
    logger.debug(
        "synthesis input\npassages:\n%s\n\ntable evidence:\n%s", "\n".join(passages), "\n".join(channel.blocks)
    )


def _batches_by_document(batches: list[BatchRef]) -> dict[int, list[BatchRef]]:
    grouped: dict[int, list[BatchRef]] = {}
    for batch in batches:
        grouped.setdefault(batch.document_id, []).append(batch)
    return grouped


async def _assemble(
    question: str,
    library_id: int,
    documents: list[DocRef],
    tables: list[TableCand],
    depths: dict[int, str],
    results: list[SqlResult],
    labels: dict[int, str],
) -> _Assembled:
    describe = [_table_render(table, labels) for table in tables]
    floor = [_doc_summary_text(doc) for doc in documents]
    reserved = sum(count_tokens(block) for block in describe) + sum(await asyncio.to_thread(count_tokens_batch, floor))
    channel = _table_channel(results, describe, max(SYNTH_BUDGET - reserved, 0))
    table_tokens = sum(count_tokens(block) for block in channel.blocks)
    batches = _batches_by_document(await load_library_batches(library_id)) if documents else {}
    passages = await render_text(question, documents, depths, batches, max(SYNTH_BUDGET - table_tokens, 0))
    return _Assembled(passages, channel.blocks, channel)


async def answer(question: str, library_id: int) -> AsyncIterator[str]:
    started = time.time()
    logger.info("query start library=%d %r", library_id, question)
    library = await load_library_name(library_id)
    documents = await load_library_documents(library_id)
    tables = await load_all_tables(library_id)
    labels = _table_labels(tables)
    coverage = await resolve(library_id, question, documents, tables, labels, library)
    covered_docs = [documents[index] for index in sorted(coverage.documents)]
    covered_tables = [tables[index] for index in sorted(coverage.tables)]
    depths = {documents[index].id: depth for index, depth in coverage.documents.items()}
    results = await run_tables(question, tables, coverage.tables, labels, library)
    logger.info(
        "coverage documents=%d/%d tables=%d/%d results=%d rows=%s",
        len(covered_docs),
        len(documents),
        len(covered_tables),
        len(tables),
        len(results),
        [result.total for result in results],
    )
    built = await _assemble(question, library_id, covered_docs, covered_tables, depths, results, labels)
    table_labels = {labels[table.table_id] for table in covered_tables}
    evidence_files = _evidence_files(built.passages) | _result_fallback_files(results, table_labels)
    _log_evidence(question, started, built.passages, results, built.channel)
    parts: list[str] = []
    async for token in synthesize(question, built.passages, built.rendered):
        parts.append(token)
        yield token
    _check_citations("".join(parts), evidence_files, table_labels)
