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
from citadel.llm import reformulate, select_evidence, synthesize, write_queries
from citadel.models.table import TableRow
from citadel.services.retrieval import TableCand, load_passages, load_tables, rerank, retrieve

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 3000
CTX_TOKENS = 32768
OUT_TOKENS = 4096
CHARS_PER_TOKEN = 4
EVIDENCE_BUDGET = (CTX_TOKENS - OUT_TOKENS - 2048) * CHARS_PER_TOKEN
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


def _safe_sql(sql: str) -> bool:
    return sql.strip().lower().startswith("select")


def _view_select(table: TableCand) -> Select[Any]:
    columns = [
        cast(TableRow.values.op("->>")(index), _DTYPE_SA.get(column.get("dtype", "string"), Text)).label(f"c{index}")
        for index, column in enumerate(table.columns)
    ]
    return select(*columns).where(TableRow.table_id == table.table_id)


def _view_cte(index: int, table: TableCand) -> str:
    compiled = _view_select(table).compile(dialect=_PG, compile_kwargs={"literal_binds": True})
    return "t" + str(index) + " AS (" + str(compiled) + ")"


SCHEMA_SAMPLES = 3


def _samples(sample_rows: list[list], index: int) -> str:
    values = [row[index] for row in sample_rows[:SCHEMA_SAMPLES] if index < len(row) and row[index] is not None]
    return f"  e.g. {', '.join(str(value) for value in values)}" if values else ""


def _schema(columns: list[dict], sample_rows: list[list]) -> str:
    lines: list[str] = []
    for index, column in enumerate(columns):
        unit = f" {column['unit']}" if column.get("unit") else ""
        label = f"c{index}: {column.get('header') or '?'} ({column.get('dtype', 'string')}{unit})"
        lines.append(label + _samples(sample_rows, index))
    return "\n".join(lines)


def _render(table: TableCand) -> str:
    headers = [column.get("header") or "?" for column in table.columns]
    return f"{table.description} | columns: {headers}"


def _schema_block(index: int, table: TableCand) -> str:
    title = table.metadata.get("title") or table.description[:60]
    return f"t{index} ({table.filename} — {title}) rows={table.n_rows}\n{_schema(table.columns, table.sample_rows)}"


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
    result = await connection.exec_driver_sql(cte + " " + sql)
    return list(result.keys()), [list(row) for row in result.fetchall()]


async def _execute(tables: list[TableCand], sql: str) -> str | None:
    sql = sql.strip().rstrip(";").strip()
    if not _safe_sql(sql):
        return None
    try:
        async with get_sessionmaker()() as session, session.begin():
            columns, rows = await _run_sql(session, tables, sql)
    except SQLAlchemyError:
        logger.warning("sql failed sql=%s\n%s", sql, traceback.format_exc())
        return None
    sources = _sources(tables, sql)
    logger.info("resolve sources=%s rows=%d sql=%s", sources, len(rows), sql)
    body = "\n".join([" | ".join(columns), *(" | ".join(str(value) for value in row) for row in rows)])
    label = ", ".join(sources) or "computed result"
    return f"[{label}]\n{body}"


@dataclass
class _Ranked:
    kind: str
    render: str
    passage: str | None
    table: TableCand | None
    score: float


def _fit(renders: list[str], budget: int) -> int:
    used = 0
    for count, render in enumerate(renders):
        used += len(render)
        if used > budget:
            return count
    return len(renders)


async def _rank(question: str, passages: list[str], candidates: list[TableCand]) -> list[_Ranked]:
    table_renders = [_render(candidate) for candidate in candidates]
    scores = await rerank(question, passages + table_renders) if (passages or candidates) else []
    ranked = [
        _Ranked("text", passage, passage, None, score)
        for passage, score in zip(passages, scores[: len(passages)], strict=True)
    ]
    ranked += [
        _Ranked("table", render, None, candidate, score)
        for candidate, render, score in zip(candidates, table_renders, scores[len(passages) :], strict=True)
    ]
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked


async def _select(question: str, passages: list[str], candidates: list[TableCand]) -> tuple[list[str], list[TableCand]]:
    ranked = await _rank(question, passages, candidates)
    fitted = ranked[: _fit([item.render for item in ranked], EVIDENCE_BUDGET)]
    chosen = set(await select_evidence(question, [item.render for item in fitted]))
    selected = [item for index, item in enumerate(fitted) if index in chosen]
    sel_passages = [item.passage for item in selected if item.kind == "text" and item.passage is not None]
    sel_tables = [item.table for item in selected if item.kind == "table" and item.table is not None]
    return sel_passages, sel_tables


async def _run_all(tables: list[TableCand], sqls: list[str]) -> list[str]:
    results: list[str] = []
    for sql in sqls:
        resolved = await _execute(tables, sql)
        if resolved is not None:
            results.append(resolved)
    return results


async def answer(question: str) -> AsyncIterator[str]:
    queries = await reformulate(question)
    hits = await retrieve(queries)
    sel_passages, sel_tables = await _select(question, await load_passages(hits.text), await load_tables(hits.tables))
    blocks = list(starmap(_schema_block, enumerate(sel_tables)))
    logger.info("schema blocks passed to slm:\n%s", "\n\n".join(blocks))
    sqls = await write_queries(question, blocks) if sel_tables else []
    logger.info(
        "query %r variants=%d text_hits=%d kept_passages=%d kept_tables=%d queries=%d",
        question[:80],
        len(queries),
        len(hits.text),
        len(sel_passages),
        len(sel_tables),
        len(sqls),
    )
    logger.info("slm sqls=%s", sqls)
    results = await _run_all(sel_tables, sqls)
    passage_budget = max(0, EVIDENCE_BUDGET - sum(len(result) for result in results))
    final_passages = sel_passages[: _fit(sel_passages, passage_budget)]
    logger.info(
        "synthesis passages=%d\npassages:\n%s\nresults:\n%s",
        len(final_passages),
        "\n".join(final_passages),
        "\n---\n".join(results),
    )
    async for token in synthesize(question, final_passages, results):
        yield token
