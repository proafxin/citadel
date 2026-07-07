import asyncio
import logging
import re
import traceback
from collections.abc import AsyncIterator
from dataclasses import dataclass
from itertools import starmap
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Numeric, Select, Text, cast, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import TypeEngine

from citadel.db import get_sessionmaker
from citadel.llm import (
    SLM_MAX_TOKENS,
    count_tokens,
    count_tokens_batch,
    reformulate,
    select_evidence,
    synthesize,
    unify_evidence,
    write_queries,
)
from citadel.models.table import TableRow
from citadel.services.retrieval import Passage, TableCand, load_passages, load_tables, retrieve

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 3000
CTX_TOKENS = 32768
OUT_TOKENS = SLM_MAX_TOKENS
BUDGET = CTX_TOKENS - OUT_TOKENS - 2048
EARLY_STOP_N = 3
UNIFY_MAX_ITEMS = 64
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
    columns = [
        cast(TableRow.values.op("->>")(index), _DTYPE_SA.get(column.get("dtype", "string"), Text)).label(f"c{index}")
        for index, column in enumerate(table.columns)
    ]
    return select(*columns).where(TableRow.table_id == table.table_id)


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


async def _filter(question: str, items: list[str]) -> list[int]:
    counts = await asyncio.to_thread(count_tokens_batch, items)
    kept: list[int] = []
    start = 0
    empty = 0
    while start < len(items):
        size = max(1, _fit(counts, start, BUDGET))
        chosen = await select_evidence(question, items[start : start + size])
        if chosen:
            kept.extend(start + index for index in chosen)
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


def _result_summary(result: SqlResult) -> str:
    sample = result.rows[:SCHEMA_SAMPLES]
    header = f"[{result.label}] {result.total} rows"
    return "\n".join([header, " | ".join(result.columns), *(_row_text(row) for row in sample)])


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


def _fit_evidence(
    passages: list[tuple[Passage, int]], results: list[tuple[SqlResult, int]], budget: int
) -> tuple[list[Passage], list[SqlResult]]:
    final_passages: list[Passage] = []
    final_results: list[SqlResult] = []
    used = 0
    for tier in (1, 2):
        fitted = _fit_results([result for result, rank in results if rank == tier], budget - used)
        final_results.extend(fitted)
        used += sum(count_tokens(_result_render(result)) for result in fitted)
        for passage, rank in passages:
            if rank != tier:
                continue
            cost = count_tokens(passage.text)
            if used + cost > budget:
                break
            final_passages.append(passage)
            used += cost
    return final_passages, final_results


async def _unify(
    question: str, passages: list[Passage], results: list[SqlResult]
) -> tuple[list[tuple[Passage, int]], list[tuple[SqlResult, int]]]:
    items = [passage.text for passage in passages] + [_result_summary(result) for result in results]
    counts = await asyncio.to_thread(count_tokens_batch, items)
    tiers: list[tuple[int, int]] = []
    start = 0
    while start < len(items):
        size = min(max(1, _fit(counts, start, BUDGET)), UNIFY_MAX_ITEMS)
        batch = await unify_evidence(question, items[start : start + size])
        tiers.extend((start + index, tier) for index, tier in batch)
        start += size
    passages_t = [(passages[index], tier) for index, tier in tiers if index < len(passages)]
    results_t = [(results[index - len(passages)], tier) for index, tier in tiers if index >= len(passages)]
    return passages_t, results_t


async def answer(question: str, library_id: int) -> AsyncIterator[str]:
    queries = await reformulate(question)
    hits = await retrieve(queries, library_id)
    passages = await load_passages(hits.text)
    candidates = await load_tables(hits.tables)
    kept_passages = [passages[index] for index in await _filter(question, [passage.text for passage in passages])]
    kept_tables = [candidates[index] for index in await _filter(question, [_table_rep(table) for table in candidates])]
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
    passages_t, results_t = await _unify(question, kept_passages, results)
    final_passages, final_results = _fit_evidence(passages_t, results_t, BUDGET)
    rendered = [_result_render(result) for result in final_results]
    logger.info(
        "synthesis passages=%d results=%d passage_ids=%s sources=%s",
        len(final_passages),
        len(final_results),
        [passage.content_id for passage in final_passages],
        [result.label for result in final_results],
    )
    async for token in synthesize(question, [passage.text for passage in final_passages], rendered):
        yield token
