import asyncio
import logging
import re
import traceback
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
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
    STRUCT_MAX_TOKENS,
    SYNTH_MAX_TOKENS,
    count_tokens,
    count_tokens_batch,
    merge_evidence,
    reformulate,
    select_evidence,
    synthesize,
    write_queries,
)
from citadel.models.table import TableRow
from citadel.services.retrieval import Passage, TableCand, load_passages, load_tables, retrieve

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 3000

# the two stages want opposite things from the window, so they no longer share a budget.
#
# FILTER_BUDGET caps how many candidates go into ONE filter call. the same candidates are read whichever way they split,
# so total prefill is identical — batch size only trades round-trips against a longer single prompt. it is NOT a KV
# concern: a request waiting for its turn allocates no cache (vLLM's scheduler reserves blocks only when it admits a
# request to the running set, so a queued one holds just its token ids), so there is no thrash to size against.
FILTER_CTX = 32768
FILTER_BUDGET = FILTER_CTX - STRUCT_MAX_TOKENS - 2048

# SYNTHESIS is ONE call, at the end, and its input budget is exactly what decides how much of the evidence reaches the
# answer — the difference between an answer the corpus supports and a thinner one. a single request can afford the whole
# window, so it gets it. must stay <= the server's --max-model-len, which counts prompt and completion TOGETHER.
SYNTH_CTX = (
    65536  # the server's whole window; it counts prompt and completion TOGETHER, so the answer comes out of this
)
SYNTH_BUDGET = SYNTH_CTX - SYNTH_MAX_TOKENS - 2048
RESULTS_BUDGET = SYNTH_BUDGET // 2  # tabular results are exact: reserve up to half the window before reducing text
EARLY_STOP_N = 3
MERGE_INPUT_BUDGET = STRUCT_MAX_TOKENS // 2  # a merge call must fit its summary in STRUCT_MAX_TOKENS; keep input under
# half that so even near-lossless (barely-compressed) output cannot overrun the cap and truncate the JSON
SCHEMA_SAMPLES = 3
SQL_ROW_CAP = 10_000  # hard ceiling on rows any generated query may return, so a broad SELECT can't pull a whole table
_PG = postgresql.dialect()
_REL_REF = re.compile(r"\b(?:from|join)\s+(\"?[A-Za-z_][A-Za-z0-9_$]*\"?)", re.IGNORECASE)
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
# stored cells are OCR/parse text; a numeric or date column routinely holds a "NULL" literal, a blank, or garbage.
# casting that straight to bigint/date THROWS and the whole generated query is dropped — so guard every non-text cast:
# cast only cells that match the type, else NULL. dates have no safe regex over OCR variance, so only their sentinels
# are nulled. this makes the typed projection total — a bad cell degrades to NULL, never an error.
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
    cleaned: Any = raw  # date / datetime: null the sentinels, then cast what remains
    for sentinel in _CELL_SENTINELS:
        cleaned = func.nullif(cleaned, sentinel)
    return cast(cleaned, sa_type).label(label)


@dataclass
class SqlResult:
    label: str
    columns: list[str]
    rows: list[list]
    total: int


def _safe_sql(sql: str) -> bool:
    return sql.strip().lower().startswith("select")


def _references_only_views(sql: str, n_tables: int) -> bool:
    # every FROM/JOIN in the generated SQL (subqueries included) must target one of our per-request t0..tn views — never
    # a base table (content, table_rows, documents, pg_*), so a prompt-injected query can't read another library's data
    allowed = {f"t{index}" for index in range(n_tables)}
    refs = [match.group(1).strip('"').lower() for match in _REL_REF.finditer(sql)]
    return bool(refs) and all(ref in allowed for ref in refs)


def _view_select(table: TableCand) -> Select[Any]:
    columns = [_typed_column(index, column.get("dtype", "string")) for index, column in enumerate(table.columns)]
    stmt = select(*columns).where(TableRow.table_id == table.table_id)
    # header rows are stored in the grid for losslessness but are NOT data — exclude them from the typed view so a query
    # over the table sees only its rows, exactly as before headers were kept
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


def _table_rep(table: TableCand) -> str:
    meta = table.metadata
    head = " | ".join(f"{key}: {meta[key]}" for key in ("title", "caption", "sheet") if meta.get(key))
    header = table.filename + (f" ({head})" if head else "")
    return f"{header}\n{_schema(table.columns, table.sample_rows)}"


def _schema_block(index: int, table: TableCand) -> str:
    return f"t{index} ({table.filename}) rows={table.n_rows}\n{_schema(table.columns, table.sample_rows)}"


def _cte(tables: list[TableCand]) -> str:
    return "WITH " + ", ".join(starmap(_view_cte, enumerate(tables)))


def _sources(tables: list[TableCand], sql: str) -> list[str]:
    used = sorted({int(match) for match in _TABLE_REF.findall(sql)})
    return list(dict.fromkeys(tables[index].filename for index in used if index < len(tables)))


async def _run_sql(session: AsyncSession, tables: list[TableCand], sql: str) -> tuple[list[str], list[list]]:
    cte = _cte(tables)
    await session.execute(text("SET TRANSACTION READ ONLY"))
    await session.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(STATEMENT_TIMEOUT_MS)})
    connection = await session.connection()
    result = await connection.exec_driver_sql(f"{cte} SELECT * FROM ({sql}) AS _capped LIMIT {SQL_ROW_CAP}")
    return list(result.keys()), [list(row) for row in result.fetchall()]


async def _execute(tables: list[TableCand], sql: str) -> SqlResult | None:
    sql = sql.strip().rstrip(";").strip()
    if not _safe_sql(sql) or not _references_only_views(sql, len(tables)):
        return None
    try:
        async with get_sessionmaker()() as session, session.begin():
            columns, rows = await _run_sql(session, tables, sql)
    except SQLAlchemyError:
        logger.warning("sql failed sql=%s\n%s", sql, traceback.format_exc())
        return None
    sources = _sources(tables, sql)
    logger.info("resolve sources=%s rows=%d sql=%s", sources, len(rows), sql)
    return SqlResult(", ".join(sources) or "computed result", columns, rows, len(rows))


async def _aggregate(question: str, tables: list[TableCand]) -> list[SqlResult]:
    blocks = list(starmap(_schema_block, enumerate(tables)))
    sqls = await write_queries(question, blocks) if tables else []
    logger.info("aggregate tables=%d sqls=%d", len(tables), len(sqls))
    results: list[SqlResult] = []
    for sql in sqls:
        resolved = await _execute(tables, sql)
        if resolved is not None:
            results.append(resolved)
    return results


def _fit(counts: list[int], start: int, budget: int) -> int:
    # how many items from `start` fit in `budget`, using token counts computed ONCE up front (no re-tokenization)
    used = 0
    for offset in range(start, len(counts)):
        used += counts[offset]
        if used > budget:
            return offset - start
    return len(counts) - start


async def _filter(question: str, items: list[str]) -> list[tuple[int, int]]:
    counts = await asyncio.to_thread(count_tokens_batch, items)
    kept: list[tuple[int, int]] = []
    start = 0
    empty = 0
    while start < len(items):
        size = max(1, _fit(counts, start, FILTER_BUDGET))
        chosen = await select_evidence(question, items[start : start + size])
        if chosen:
            kept.extend((start + index, score) for index, score in chosen)
            empty = 0
        else:
            empty += 1
            if empty >= EARLY_STOP_N:
                break
        start += size
    return kept


def _row_text(row: list) -> str:
    return " | ".join("" if value is None else str(value) for value in row)


def _result_render(result: SqlResult) -> str:
    shown = len(result.rows)
    head = f"[{result.label}]" if shown >= result.total else f"[{result.label}] showing {shown} of {result.total} rows"
    return "\n".join([head, " | ".join(result.columns), *(_row_text(row) for row in result.rows)])


def _fit_results(results: list[SqlResult], budget: int) -> list[SqlResult]:
    if not results or budget <= 0:
        return []
    kept: list[list[list]] = [[] for _ in results]
    used = sum(count_tokens(f"[{result.label}]\n" + " | ".join(result.columns)) for result in results)
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
    return [SqlResult(r.label, r.columns, kept[index], r.total) for index, r in enumerate(results) if kept[index]]


@dataclass
class _Evidence:
    text: str
    sources: list[int]
    tokens: int
    score: int
    heading: str | None
    document_id: int


def _group_key(evidence: _Evidence, level: int) -> object:
    # level 0 merges within a section (blocks under one heading), level 1 within a document, level 2+ merges anything.
    # escalating the grain only when a finer merge did not free enough keeps "do not summarize a summary" as far as the
    # budget allows
    if level == 0:
        return (evidence.document_id, evidence.heading)
    if level == 1:
        return evidence.document_id
    return 0


def _chunk_by_tokens(items: list[_Evidence], budget: int) -> list[list[_Evidence]]:
    # a sibling group larger than one merge call's context is split so each call fits; an item bigger than budget on
    # its own goes alone (a single oversized passage is summarized by itself). all chunks stay within the one group
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
    summary = await merge_evidence(question, [evidence.text for evidence in chunk])
    sources = [source for evidence in chunk for source in evidence.sources]
    if summary:
        tokens = count_tokens(summary)
    else:  # merge failed: concatenate verbatim so no source is lost (tokens do not shrink → reduce escalates)
        summary = "\n".join(evidence.text for evidence in chunk)
        tokens = sum(evidence.tokens for evidence in chunk)
    return _Evidence(summary, sources, tokens, max(e.score for e in chunk), chunk[0].heading, chunk[0].document_id)


async def _reduce(question: str, evidences: list[_Evidence], budget: int, level: int) -> list[_Evidence]:
    total = sum(evidence.tokens for evidence in evidences)
    if total <= budget:
        return evidences
    overflow = total - budget
    order = sorted(range(len(evidences)), key=lambda index: (evidences[index].score, -evidences[index].tokens))
    # merging can only shrink, so freeing `overflow` needs at least `overflow` tokens of material on the table. mark the
    # lowest-score evidence up to that; if the merge does not shrink enough, the recursion escalates and marks more
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
    if sum(evidence.tokens for evidence in combined) >= total:  # no progress (merge produced nothing shorter) → stop
        return combined
    return await _reduce(question, combined, budget, level + 1)


async def _reduce_passages(question: str, passages: list[Passage], budget: int) -> list[_Evidence]:
    if not passages:
        return []
    counts = await asyncio.to_thread(count_tokens_batch, [passage.text for passage in passages])
    evidences = [
        _Evidence(
            passage.text,
            [passage.content_id],
            counts[index],
            passage.score,
            passage.heading,
            passage.document_id,
        )
        for index, passage in enumerate(passages)
    ]
    return await _reduce(question, evidences, budget, 0)


async def answer(question: str, library_id: int) -> AsyncIterator[str]:
    queries = await reformulate(question)
    logger.info("reformulate %r -> %s", question[:80], queries)
    hits = await retrieve(queries, library_id)
    passages = await load_passages(hits.text)
    candidates = await load_tables(hits.tables)
    async with asyncio.TaskGroup() as group:
        passage_filter = group.create_task(_filter(question, [passage.text for passage in passages]))
        table_filter = group.create_task(_filter(question, [_table_rep(table) for table in candidates]))
    kept_passages = [replace(passages[index], score=score) for index, score in passage_filter.result()]
    kept_tables = [candidates[index] for index, _ in table_filter.result()]
    results = await _aggregate(question, kept_tables)
    logger.info(
        "query %r variants=%d text_hits=%d table_hits=%d kept_passages=%d kept_tables=%d results=%d",
        question[:80],
        len(queries),
        len(hits.text),
        len(hits.tables),
        len(kept_passages),
        len(kept_tables),
        len(results),
    )
    logger.info(
        "kept passage_ids=%s table_ids=%s",
        [passage.content_id for passage in kept_passages],
        [table.content_id for table in kept_tables],
    )
    fitted_results = _fit_results(results, RESULTS_BUDGET)
    rendered = [_result_render(result) for result in fitted_results]
    passage_budget = max(SYNTH_BUDGET - sum(count_tokens(block) for block in rendered), 0)
    evidences = await _reduce_passages(question, kept_passages, passage_budget)
    covered = {source for evidence in evidences for source in evidence.sources}
    uncovered = [passage.content_id for passage in kept_passages if passage.content_id not in covered]
    if uncovered:
        logger.error("reduction dropped sources uncovered=%d ids=%s", len(uncovered), uncovered)
    logger.info(
        "synthesis evidences=%d results=%d merged=%d source_count=%d uncovered=%d",
        len(evidences),
        len(fitted_results),
        sum(1 for evidence in evidences if len(evidence.sources) > 1),
        len(covered),
        len(uncovered),
    )
    async for token in synthesize(question, [evidence.text for evidence in evidences], rendered):
        yield token
