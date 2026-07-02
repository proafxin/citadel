import logging
import re
import traceback
from collections.abc import AsyncIterator
from itertools import starmap
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Numeric, Select, Text, TypeEngine, cast, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from citadel.db import get_sessionmaker
from citadel.llm import filter_tables, reformulate, synthesize, write_queries
from citadel.models.table import TableRow
from citadel.services.retrieval import TableCand, load_passages, load_tables, retrieve

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 3000
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


def _schema(columns: list[dict]) -> str:
    lines: list[str] = []
    for index, column in enumerate(columns):
        unit = f" {column['unit']}" if column.get("unit") else ""
        lines.append(f"c{index}: {column.get('header') or '?'} ({column.get('dtype', 'string')}{unit})")
    return "\n".join(lines)


def _render(table: TableCand) -> str:
    headers = [column.get("header") or "?" for column in table.columns]
    return f"{table.description} | columns: {headers}"


def _schema_block(index: int, table: TableCand) -> str:
    title = table.metadata.get("title") or table.description[:60]
    return f"t{index} ({table.filename} — {title}) rows={table.n_rows}\n{_schema(table.columns)}"


def _cte(tables: list[TableCand]) -> str:
    return "WITH " + ", ".join(starmap(_view_cte, enumerate(tables)))


def _sources(tables: list[TableCand], sql: str) -> list[str]:
    used = sorted({int(match) for match in _TABLE_REF.findall(sql)})
    return list(dict.fromkeys(tables[index].filename for index in used if index < len(tables)))


async def _run_sql(session: AsyncSession, tables: list[TableCand], sql: str) -> tuple[list[str], list[list]]:
    cte = _cte(tables)
    connection = await session.connection()
    await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
    await connection.exec_driver_sql("SELECT set_config('statement_timeout', %s, true)", (str(STATEMENT_TIMEOUT_MS),))
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


async def answer(question: str) -> AsyncIterator[str]:
    queries = await reformulate(question)
    hits = await retrieve(queries)
    passages = await load_passages(hits.text)
    candidates = await load_tables(hits.tables)
    keep = await filter_tables(question, [_render(candidate) for candidate in candidates])
    kept = [candidates[index] for index in keep]
    blocks = list(starmap(_schema_block, enumerate(kept)))
    sqls = await write_queries(question, blocks)
    logger.info(
        "query %r variants=%d text_hits=%d table_cands=%d kept=%d queries=%d",
        question[:80],
        len(queries),
        len(hits.text),
        len(candidates),
        len(kept),
        len(sqls),
    )
    results: list[str] = []
    for sql in sqls:
        resolved = await _execute(kept, sql)
        if resolved is not None:
            results.append(resolved)
    async for token in synthesize(question, passages, results):
        yield token
