import asyncio
import logging
import re
import time
import traceback
from collections.abc import AsyncIterator
from dataclasses import dataclass
from itertools import starmap
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Numeric, Select, Text, case, cast, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import TypeEngine

from citadel.db import get_sessionmaker
from citadel.llm import (
    RELEVANCE_BUDGET,
    SLM_MODEL_LEN,
    STRUCT_MAX_TOKENS,
    SYNTH_MAX_TOKENS,
    call_level1_relevance,
    call_level2_relevance,
    call_stage1_relevance,
    count_tokens_batch,
    merge_evidence,
    relevance_prompt_tokens,
    synthesize,
    write_final_report,
    write_queries,
)
from citadel.models.table import TableRow
from citadel.schemas.query import QueryPlan
from citadel.services.retrieval import (
    BatchRef,
    DocRef,
    TableCand,
    load_all_tables,
    load_block_texts,
    load_library_batches,
    load_library_documents,
    load_library_name,
    scope_text_block_ids,
)
from citadel.services.search import rank_content

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 3000


SYNTH_BUDGET = SLM_MODEL_LEN - SYNTH_MAX_TOKENS - 2048
MERGE_INPUT_BUDGET = STRUCT_MAX_TOKENS // 2
SCHEMA_SAMPLES = 3
LEVEL2_STOP_STREAK = 2
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
    return await write_queries(question, blocks, library)


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


def _chunk_texts(texts: list[str], counts: list[int], budget: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    used = 0
    for item, tokens in zip(texts, counts, strict=True):
        if current and used + tokens > budget:
            chunks.append(current)
            current, used = [], 0
        current.append(item)
        used += tokens
    if current:
        chunks.append(current)
    return chunks


async def _summarize_texts(question: str, texts: list[str]) -> str | None:
    if not texts:
        return None
    counts = await asyncio.to_thread(count_tokens_batch, texts)
    if sum(counts) <= MERGE_INPUT_BUDGET:
        return await merge_evidence(question, texts) or "\n".join(texts)
    chunks = _chunk_texts(texts, counts, MERGE_INPUT_BUDGET)
    reduced = await asyncio.gather(*(_summarize_texts(question, chunk) for chunk in chunks))
    return await _summarize_texts(question, [text for text in reduced if text])


def _doc_item(doc: DocRef) -> str:
    return f"document — {doc.filename}: {doc.summary}"


def _table_item(table: TableCand, labels: dict[int, str]) -> str:
    named = [str(table.metadata[key]) for key in ("title", "caption") if table.metadata.get(key)]
    columns = ", ".join(str(column.get("header") or "?") for column in table.columns)
    head = f"table — {_table_label(table, labels)} rows={table.n_rows}"
    return f"{head}: {' | '.join(named)}. columns {columns}" if named else f"{head}: columns {columns}"


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


async def _fits_or_summarized(question: str, texts: list[str], counts: list[int], budget: int) -> list[str] | None:
    if sum(counts) <= budget:
        return None
    summary = await _summarize_texts(question, texts)
    return [summary] if summary else []


def _stage1_pack(documents: list[DocRef], question: str) -> list[list[DocRef]]:
    batches: list[list[DocRef]] = []
    current: list[DocRef] = []
    for doc in documents:
        candidate = [*current, doc]
        tokens = relevance_prompt_tokens("stage1_relevance", question, [_doc_item(d) for d in candidate])
        if current and tokens > RELEVANCE_BUDGET:
            batches.append(current)
            current = [doc]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


async def stage1_relevant_documents(question: str, documents: list[DocRef]) -> list[DocRef]:
    if not documents:
        return []
    batches = _stage1_pack(documents, question)
    picks = await asyncio.gather(
        *(
            call_stage1_relevance(question, [_doc_item(doc) for doc in batch], f"stage1:{batch_no}")
            for batch_no, batch in enumerate(batches)
        )
    )
    relevant = [batch[index] for batch, indices in zip(batches, picks, strict=True) for index in indices]
    logger.info("stage1 documents=%d relevant=%d", len(documents), len(relevant))
    return relevant


async def level0_gate(question: str, documents: list[DocRef], budget: int) -> list[str] | None:
    texts = [_doc_summary_text(doc) for doc in documents]
    counts = await asyncio.to_thread(count_tokens_batch, texts)
    reduced = await _fits_or_summarized(question, texts, counts, budget)
    if reduced is not None:
        logger.info("level0 gate closed documents=%d", len(documents))
    return reduced


def _batch_item(batch: BatchRef) -> str:
    return f"batch — {_batch_cite(batch)}: {batch.summary}"


async def level1_relevant(
    question: str, batches: list[BatchRef], tables: list[TableCand], labels: dict[int, str]
) -> tuple[list[BatchRef], list[TableCand]]:
    if not batches and not tables:
        return [], []
    items = [_batch_item(batch) for batch in batches] + [_table_item(table, labels) for table in tables]
    tokens = relevance_prompt_tokens("level1_relevance", question, items)
    if tokens > RELEVANCE_BUDGET:
        logger.warning("level1 inventory does not fit one call tokens=%d budget=%d", tokens, RELEVANCE_BUDGET)
    batch_indices, table_indices = await call_level1_relevance(question, items, "level1")
    relevant_batches = [batches[index] for index in batch_indices if index < len(batches)]
    relevant_tables = [tables[index] for index in table_indices if index < len(tables)]
    return relevant_batches, relevant_tables


async def level1_gate(
    question: str, batches: list[BatchRef], tables: list[TableCand], labels: dict[int, str], budget: int
) -> list[str] | None:
    texts = [_summary_text(batch) for batch in batches] + [_table_render(table, labels) for table in tables]
    counts = await asyncio.to_thread(count_tokens_batch, texts)
    reduced = await _fits_or_summarized(question, texts, counts, budget)
    if reduced is not None:
        logger.info("level1 gate closed batches=%d tables=%d", len(batches), len(tables))
    return reduced


def _window_by_tokens(ids: list[int], counts: dict[int, int], budget: int) -> list[list[int]]:
    windows: list[list[int]] = []
    current: list[int] = []
    used = 0
    for content_id in ids:
        tokens = counts[content_id]
        if current and used + tokens > budget:
            windows.append(current)
            current, used = [], 0
        current.append(content_id)
        used += tokens
    if current:
        windows.append(current)
    return windows


def _batch_for_block(batches: list[BatchRef], document_id: int, block_ordinal: int) -> BatchRef | None:
    for batch in batches:
        if batch.document_id == document_id and batch.start_block_ordinal <= block_ordinal <= batch.end_block_ordinal:
            return batch
    return None


async def level2_relevant_texts(question: str, library_id: int, batches: list[BatchRef]) -> list[str]:
    if not batches:
        return []
    ranges = [(batch.document_id, batch.start_block_ordinal, batch.end_block_ordinal) for batch in batches]
    content_ids = await scope_text_block_ids(ranges)
    if not content_ids:
        return []
    ordered = await rank_content(question, content_ids, key=f"level2:{library_id}")
    blocks = await load_block_texts(ordered)
    ordered = [content_id for content_id in ordered if content_id in blocks]
    counts = dict(
        zip(ordered, await asyncio.to_thread(count_tokens_batch, [blocks[cid].text for cid in ordered]), strict=True)
    )
    windows = _window_by_tokens(ordered, counts, RELEVANCE_BUDGET)
    texts: list[str] = []
    empty_streak = 0
    walked = 0
    for index, window in enumerate(windows):
        picks = await call_level2_relevance(
            question, [blocks[cid].text for cid in window], f"level2:{library_id}:{index}"
        )
        walked += 1
        if not picks:
            empty_streak += 1
            if empty_streak >= LEVEL2_STOP_STREAK:
                break
            continue
        empty_streak = 0
        for pick in picks:
            if pick >= len(window):
                continue
            block = blocks[window[pick]]
            batch = _batch_for_block(batches, block.document_id, block.block_ordinal)
            citation = _batch_cite(batch) if batch is not None else block.document_id
            texts.append(f"[{citation}] {block.text}")
    logger.info("level2 pooled=%d windows=%d/%d relevant=%d", len(content_ids), walked, len(windows), len(texts))
    return texts


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


async def _respond(question: str, started: float, passages: list[str], results: list[str]) -> AsyncIterator[str]:
    parts: list[str] = []
    async for token in synthesize(question, passages, results):
        parts.append(token)
        yield token
    logger.info(
        "query done %r passages=%d results=%d %.1fs", question[:80], len(passages), len(results), time.time() - started
    )
    _check_citations("".join(parts), _evidence_files(passages), set())


async def _consume(stream: AsyncIterator[str]) -> str:
    return "".join([token async for token in stream])


async def _string_stream(text: str) -> AsyncIterator[str]:
    yield text


async def _table_report(question: str, result_blocks: list[str]) -> str:
    if not result_blocks:
        return ""
    return await _consume(synthesize(question, [], result_blocks))


async def _level2_respond(
    question: str,
    started: float,
    library_id: int,
    relevant_batches: list[BatchRef],
    relevant_tables: list[TableCand],
    labels: dict[int, str],
    library: str,
) -> AsyncIterator[str]:
    texts, results = await asyncio.gather(
        level2_relevant_texts(question, library_id, relevant_batches),
        _aggregate(question, relevant_tables, labels, library),
    )
    result_blocks = [_result_render(result) for result in results]
    text_report, table_report = await asyncio.gather(
        _summarize_texts(question, texts), _table_report(question, result_blocks)
    )
    table_labels = {labels[table.table_id] for table in relevant_tables}
    if text_report and table_report:
        stream = write_final_report(question, text_report, table_report)
    elif text_report:
        stream = _string_stream(text_report)
    elif table_report:
        stream = _string_stream(table_report)
    else:
        stream = synthesize(question, [], [])
    parts: list[str] = []
    async for token in stream:
        parts.append(token)
        yield token
    logger.info(
        "query done %r batches=%d tables=%d results=%d %.1fs",
        question[:80],
        len(relevant_batches),
        len(relevant_tables),
        len(results),
        time.time() - started,
    )
    evidence_files = _evidence_files(texts) | _result_fallback_files(results, table_labels)
    _check_citations("".join(parts), evidence_files, table_labels)


async def answer(question: str, library_id: int) -> AsyncIterator[str]:
    started = time.time()
    logger.info("query start library=%d %r", library_id, question)
    library = await load_library_name(library_id)
    documents = await load_library_documents(library_id)

    necessary = await stage1_relevant_documents(question, documents)
    if not necessary:
        async for token in _respond(question, started, [], []):
            yield token
        return

    floor_report = await level0_gate(question, necessary, SYNTH_BUDGET)
    if floor_report is not None:
        async for token in _respond(question, started, floor_report, []):
            yield token
        return

    necessary_ids = {doc.id for doc in necessary}
    tables = [table for table in await load_all_tables(library_id) if table.document_id in necessary_ids]
    labels = _table_labels(tables)
    batches = [batch for batch in await load_library_batches(library_id) if batch.document_id in necessary_ids]

    relevant_batches, relevant_tables = await level1_relevant(question, batches, tables, labels)
    level1_report = await level1_gate(question, relevant_batches, relevant_tables, labels, SYNTH_BUDGET)
    if level1_report is not None:
        async for token in _respond(question, started, level1_report, []):
            yield token
        return

    respond = _level2_respond(question, started, library_id, relevant_batches, relevant_tables, labels, library)
    async for token in respond:
        yield token
