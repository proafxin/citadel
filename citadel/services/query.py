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
    STRUCT_MAX_TOKENS,
    SYNTH_MAX_TOKENS,
    collect_select,
    count_tokens,
    count_tokens_batch,
    emit_select,
    merge_evidence,
    synthesize,
    write_queries,
)
from citadel.models.table import TableRow
from citadel.schemas.query import QueryPlan
from citadel.services.retrieval import (
    BatchRef,
    TableCand,
    load_all_tables,
    load_block_texts,
    load_library_batches,
    load_library_name,
    scope_block_ids,
)

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 3000

# SELECT_BUDGET caps how many batch summaries go into ONE selection call. the summaries are read whichever way they
# split, so total prefill is identical — batch size only trades round-trips against a longer single prompt.
SELECT_CTX = 32768
SELECT_BUDGET = SELECT_CTX - STRUCT_MAX_TOKENS - 2048

# SYNTHESIS is ONE call, at the end, and its input budget is exactly what decides how much of the evidence reaches the
# answer — the difference between an answer the corpus supports and a thinner one. a single request can afford the whole
# window, so it gets it. must stay <= the server's --max-model-len, which counts prompt and completion TOGETHER.
SYNTH_CTX = (
    65536  # the server's whole window; it counts prompt and completion TOGETHER, so the answer comes out of this
)
SYNTH_BUDGET = SYNTH_CTX - SYNTH_MAX_TOKENS - 2048
MERGE_INPUT_BUDGET = STRUCT_MAX_TOKENS // 2  # a merge call must fit its summary in STRUCT_MAX_TOKENS; keep input under
# half that so even near-lossless (barely-compressed) output cannot overrun the cap and truncate the JSON
SCHEMA_SAMPLES = 3
# what the table descriptions (file, row count, schema) may take of the synthesis budget. they are attached for every
# table, not just the queried ones, so the answer always knows what data exists — but they are DESCRIPTIONS, and must
# never crowd out the rows themselves or the passages, hence a bounded share rather than the whole remainder
TABLE_REP_BUDGET = 8192
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
_COL_REF = re.compile(r"\bt(\d+)\.c(\d+)\b")  # a qualified column ref; rewritten to its header name for readable sql
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
    refs: list[int]  # the table indices this query read, so its source tables can be shown alongside the rows
    query: str  # the executed query, column refs rewritten to header names — the result's provenance. a lone value
    # under a model-chosen alias (measured: SUM filtered to two conditions, aliased `total_sales`) reads as a grand
    # total; the query states what was actually computed, so a filtered figure can never be mistaken for the whole


def _safe_sql(sql: str) -> bool:
    return sql.strip().lower().startswith("select")


def _references_only_views(sql: str, n_tables: int) -> bool:
    # every FROM/JOIN in the generated SQL (subqueries included) must target one of our per-request t0..tn views or the
    # catalog relation — never a base table (content, table_rows, documents, pg_*), so a prompt-injected query can't
    # read another library's data
    allowed = {"catalog"} | {f"t{index}" for index in range(n_tables)}
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


def _schema_block(index: int, table: TableCand) -> str:
    return f"t{index} ({_table_label(table)}) rows={table.n_rows}\n{_schema(table.columns, table.sample_rows)}"


def _table_locator(table: TableCand) -> str:
    # a table is located by whatever its source actually has: a spreadsheet by sheet, a table inside a document by page,
    # a plain tabular file by nothing — the file IS the table. this is the same granularity the answer cites at
    sheet = table.metadata.get("sheet")
    if sheet:
        return str(sheet)
    return f"p{table.page_no}" if table.page_no is not None else ""


def _table_label(table: TableCand) -> str:
    # the ONE name a table answers to, on both its description and any result computed from it. it is built from real
    # source identity rather than the per-request t0..tn labels, because the answer cites these labels back and a
    # request-local index means nothing to a reader. it stays file + locator and nothing else: a label is parsed back
    # out of the answer to check citations, so anything with a comma in it would break that read
    locator = _table_locator(table)
    return f"{table.filename} ({locator})" if locator else table.filename


def _table_render(table: TableCand) -> str:
    # a table's description as the ANSWER needs it, and no more: which file it came from, where in it, what it is, how
    # big. no schema, no samples, no dtypes — nothing here writes SQL any more, the columns that matter already arrive
    # as the result's own header row, and a sample value would just be a concrete-looking number beside the real result,
    # quotable as if it were one. what this adds is only what a bare result lacks: attribution and the scale behind it
    meta = table.metadata
    described = [str(meta[key]) for key in ("title", "caption") if meta.get(key)]
    described.extend(str(note) for note in meta.get("notes") or [])
    head = f"[{_table_label(table)}] rows={table.n_rows}"
    return f"{head} {' | '.join(described)}" if described else head


def _fit_tables(tables: list[TableCand], relevant: list[int], budget: int) -> list[str]:
    # ONLY the tables named relevant for this question. the query-writing call already saw every table in the library —
    # that is how relevance gets decided at all — so by now it is decided, and describing the rest would pay a second
    # time for tables that bear on nothing
    blocks: list[str] = []
    spent = 0
    for index in relevant:
        block = _table_render(tables[index])
        cost = count_tokens(block) + 1
        if spent + cost > budget:
            continue
        blocks.append(block)
        spent += cost
    return blocks


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_CATALOG_SQL = "catalog AS (SELECT * FROM (VALUES %s) AS _cat(file, sheet, row_count))"


def _catalog_cte(tables: list[TableCand]) -> str:
    # the corpus's own shape as a QUERYABLE relation: one row per table, holding what a question about the tables
    # themselves needs — which file and sheet, and how many rows. a ranking or count over the tables ("which is
    # largest", "how many rows does X have", "how many tables") is then a real SELECT, not the model eyeballing a list
    # of row counts across a hundred descriptions. built here from the loaded tables — filenames and integer counts we
    # hold, never model input — with every string quote-escaped, so it carries no injection surface
    values = ", ".join(
        f"({_sql_literal(table.filename)}, {_sql_literal(str(table.metadata.get('sheet') or ''))}, {table.n_rows})"
        for table in tables
    )
    return _CATALOG_SQL % values


def _cte(tables: list[TableCand]) -> str:
    return "WITH " + ", ".join([_catalog_cte(tables), *starmap(_view_cte, enumerate(tables))])


def _refs(tables: list[TableCand], sql: str) -> list[int]:
    return sorted({index for match in _TABLE_REF.findall(sql) if (index := int(match)) < len(tables)})


def _col_name(tables: list[TableCand], table: int, col: int) -> str:
    if table < len(tables) and col < len(tables[table].columns):
        return str(tables[table].columns[col].get("header") or f"c{col}")
    return f"c{col}"


def _readable_sql(tables: list[TableCand], sql: str) -> str:
    # rewrite the executed query into what a reader can follow: every `tN.cM` becomes its header name and every table
    # reference becomes the table's own label. no internal position ids survive, and the WHERE that produced the value
    # is now stated in the reader's own vocabulary — this is the provenance a lone aggregate value cannot carry itself
    sql = _COL_REF.sub(lambda m: _col_name(tables, int(m[1]), int(m[2])), sql)
    return _TABLE_REF.sub(lambda m: _table_label(tables[int(m[1])]) if int(m[1]) < len(tables) else m[0], sql)


def _sources(tables: list[TableCand], refs: list[int]) -> list[str]:
    return list(dict.fromkeys(_table_label(tables[index]) for index in refs))


def _catalog_sources(columns: list[str], rows: list[list]) -> list[str]:
    # a catalog query names no tN view, so it has no refs to source from — but its RESULT rows carry the `file` (and,
    # when present, `sheet`) of the tables it picked out. those are the real subject of the answer ("the largest is
    # sales_test.csv"), so the label is built from them rather than left as an anonymous "computed result"
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
        logger.warning("sql rejected (not a safe view-only SELECT) sql=%s", sql)
        return None
    try:
        async with get_sessionmaker()() as session, session.begin():
            columns, rows = await _run_sql(session, tables, sql)
    except SQLAlchemyError:
        logger.warning("sql failed sql=%s\n%s", sql, traceback.format_exc())
        return None
    refs = _refs(tables, sql)
    sources = _sources(tables, refs) or _catalog_sources(columns, rows)
    logger.info("resolve sources=%s rows=%d sql=%s", sources, len(rows), sql)
    return SqlResult(
        ", ".join(sources) or "computed result", columns, rows, len(rows), refs, _readable_sql(tables, sql)
    )


async def _aggregate(question: str, tables: list[TableCand], library: str = "") -> tuple[list[SqlResult], list[int]]:
    blocks = list(starmap(_schema_block, enumerate(tables)))
    plan = await write_queries(question, blocks, library) if tables else QueryPlan()
    results: list[SqlResult] = []
    for sql in plan.queries:
        resolved = await _execute(tables, sql)
        if resolved is not None:
            results.append(resolved)
    # a table is described if it was NAMED relevant or if a query actually read it. the union matters: a query can name
    # a table the model forgot to mark, and a rejected or failed query must not take its table's metadata down with it
    relevant = list(dict.fromkeys([*plan.tables, *(index for result in results for index in result.refs)]))
    logger.info(
        "aggregate tables=%d sqls=%d ran=%d relevant=%s", len(tables), len(plan.queries), len(results), relevant
    )
    return results, relevant


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


def _table_channel(results: list[SqlResult], tables: list[TableCand], relevant: list[int]) -> _TableChannel:
    # the table channel's whole evidence: the relevant tables' descriptions, then the rows the queries computed. rows
    # are fitted first because they are the exact answer and must not lose ground to a description, and the descriptions
    # are then laid in ahead of them — what a table IS has to be readable before what was pulled out of it, and both
    # carry the same label, so a result and its source table are one name in one place
    fitted = _fit_results(results, SYNTH_BUDGET - TABLE_REP_BUDGET)
    result_blocks = [_result_render(result) for result in fitted]
    spent = sum(count_tokens(block) for block in result_blocks)
    describe = _fit_tables(tables, relevant, min(TABLE_REP_BUDGET, SYNTH_BUDGET - spent))
    return _TableChannel([*describe, *result_blocks], fitted, len(describe))


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


def _pack(counts: list[int], budget: int) -> list[list[int]]:
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


async def select_batches(question: str, library_id: int, library: str = "") -> list[BatchRef]:
    # the text channel: the SLM reads every batch summary in the library (split across calls only if they overflow one)
    # and returns the batches mandatory to answer, in relevance order. this is the whole scope-reduction for text
    batches = await load_library_batches(library_id)
    if not batches:
        return []
    items = [f"{batch.filename}\n{batch.summary}" for batch in batches]
    counts = await asyncio.to_thread(count_tokens_batch, items)
    packs = _pack(counts, SELECT_BUDGET)
    jobs = [(pack, await emit_select(question, [items[i] for i in pack], library)) for pack in packs]
    selected: list[BatchRef] = []
    for index, (pack, job_id) in enumerate(jobs):
        chosen = await collect_select(job_id, len(pack))
        logger.info("select pack=%d chose=%d/%d", index, len(chosen), len(pack))
        selected.extend(batches[pack[local]] for local in chosen)
    logger.info(
        "select library=%d batches=%d packs=%d selected=%d labels=%s",
        library_id,
        len(batches),
        len(packs),
        len(selected),
        [_batch_cite(batch) for batch in selected],
    )
    return selected


async def run_tables(
    question: str, library_id: int, library: str
) -> tuple[list[SqlResult], list[TableCand], list[int]]:
    # the table channel: the SLM sees every table's schema, samples and ROW COUNT, then names the tables the question
    # bears on and writes the queries for whatever must be computed from their rows. the tables come back too — their
    # metadata is ours to attach, never something to spend a query retrieving
    tables = await load_all_tables(library_id)
    results, relevant = await _aggregate(question, tables, library)
    return results, tables, relevant


def _batch_cite(batch: BatchRef) -> str:
    # a summary covers a page range, so it cites like a passage does — a page span, never any internal unit name. a
    # tabular batch has no page (its content is sheets), so it cites by filename alone
    if batch.start_page_no is None:
        return batch.filename
    if batch.end_page_no and batch.end_page_no != batch.start_page_no:
        return f"{batch.filename} p{batch.start_page_no}-p{batch.end_page_no}"
    return f"{batch.filename} p{batch.start_page_no}"


def _summary_text(batch: BatchRef) -> str:
    return f"[{_batch_cite(batch)}] {batch.summary}"


def _summary_evidence(batch: BatchRef, rank: int, total: int, tokens: int) -> _Evidence:
    return _Evidence(_summary_text(batch), [batch.id], tokens, total - rank, None, batch.document_id)


async def _reduce_summaries(question: str, batches: list[BatchRef], budget: int) -> list[str]:
    # summaries are already a reduction, so this only fires when even the SELECTED summaries overflow the budget — the
    # large-corpus / very-broad case. it reduces the least-relevant summaries first (emission order is relevance)
    texts = [_summary_text(batch) for batch in batches]
    counts = await asyncio.to_thread(count_tokens_batch, texts)
    if sum(counts) <= budget:
        return texts
    evidences = [_summary_evidence(batch, rank, len(batches), counts[rank]) for rank, batch in enumerate(batches)]
    return [evidence.text for evidence in await _reduce(question, evidences, budget, 0)]


async def render_text(question: str, batches: list[BatchRef], budget: int) -> list[str]:
    # affordability decides summary-vs-block. every batch stores content_tokens (the full size of its blocks), so
    # whether the selected blocks fit is known up front, before any ranking. if they fit, show them ALL — there is no
    # subset to pick, so no filter runs and nothing can overflow. if they do not fit, the query pulled in more content
    # than the window holds (a broad question over a large corpus), so fall back to the summaries, which are the
    # compressed form of exactly that content
    if not batches or budget <= 0:
        logger.info("text mode=empty batches=%d budget=%d", len(batches), budget)
        return []
    content = sum(batch.content_tokens for batch in batches)
    if content > budget:  # fast reject before loading anything
        summaries = await _reduce_summaries(question, batches, budget)
        logger.info(
            "text mode=summary reason=content batches=%d content=%d budget=%d out=%d",
            len(batches),
            content,
            budget,
            len(summaries),
        )
        return summaries
    ranges = [(batch.document_id, batch.start_block_ordinal, batch.end_block_ordinal) for batch in batches]
    texts = await load_block_texts(await scope_block_ids(ranges))
    blocks = sorted(texts.values(), key=lambda block: (block.document_id, block.block_ordinal))
    rendered = [block.text for block in blocks]
    # content_tokens counts the block bodies; the rendered passages add a "[filename pN]" prefix each, so the real total
    # runs larger. verify against the ACTUAL size and fall back to summaries if the prefixes tip it past the budget
    actual = sum(await asyncio.to_thread(count_tokens_batch, rendered))
    if actual <= budget:
        per_doc: dict[int, int] = {}
        for block in blocks:
            per_doc[block.document_id] = per_doc.get(block.document_id, 0) + 1
        logger.info(
            "text mode=blocks batches=%d blocks=%d content=%d actual=%d budget=%d per_doc=%s",
            len(batches),
            len(rendered),
            content,
            actual,
            budget,
            per_doc,
        )
        logger.debug("text blocks:\n%s", "\n".join(rendered))
        return rendered
    summaries = await _reduce_summaries(question, batches, budget)
    logger.info(
        "text mode=summary reason=rendered batches=%d blocks=%d actual=%d budget=%d out=%d",
        len(batches),
        len(rendered),
        actual,
        budget,
        len(summaries),
    )
    return summaries


_CITE = re.compile(r"\[([^\]]+)\]")


def _cite_file(label: str) -> str:
    # strip the locator off a citation label so "sales.csv p3-p5", "sales.xlsx (Sheet1)" and the bare filename all
    # compare as the same source — the check is whether the FILE was in evidence, not which part of it
    return re.sub(r"\s+p\d.*$", "", re.sub(r"\s*\([^)]*\)\s*$", "", label)).strip()


def _evidence_files(passages: list[str], results: list[str]) -> set[str]:
    files: set[str] = set()
    for text in passages:
        match = _CITE.match(text)
        if match:
            files.add(_cite_file(match.group(1)))
    for text in results:  # a result label is a comma-joined list of its source filenames
        match = _CITE.match(text)
        if match:
            files.update(_cite_file(part) for part in match.group(1).split(", "))
    return files


def _check_citations(response: str, evidence_files: set[str]) -> None:
    # every citation in the answer must point at a document we actually gave the model. one that does not is a
    # fabricated source — the clearest hallucination signal we can check automatically
    cited = {_cite_file(label) for label in _CITE.findall(response)}
    fabricated = sorted(cited - evidence_files)
    if fabricated:
        logger.error("HALLUCINATION cited sources not in evidence=%s | evidence=%s", fabricated, sorted(evidence_files))
    logger.info("answer chars=%d cited_files=%d fabricated=%d", len(response), len(cited), len(fabricated))
    logger.debug("answer:\n%s", response)


def _log_evidence(
    question: str,
    started: float,
    batches: list[BatchRef],
    passages: list[str],
    results: list[SqlResult],
    tables: list[TableCand],
    channel: _TableChannel,
) -> None:
    table_tokens = sum(count_tokens(block) for block in channel.blocks)
    logger.info(
        "query done %r batches=%d results=%d/%d described=%d/%d table_tokens=%d dropped_rows=%d "
        "passages=%d synth_tokens=%d/%d %.1fs",
        question[:80],
        len(batches),
        len(channel.fitted),
        len(results),
        channel.described,
        len(tables),
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


async def answer(question: str, library_id: int) -> AsyncIterator[str]:
    started = time.time()
    logger.info("query start library=%d %r", library_id, question)
    library = await load_library_name(library_id)
    async with asyncio.TaskGroup() as group:
        text_task = group.create_task(select_batches(question, library_id, library))
        table_task = group.create_task(run_tables(question, library_id, library))
    batches = text_task.result()
    results, tables, relevant = table_task.result()
    logger.info(
        "channels text_batches=%d docs=%s | table_results=%d rows=%s relevant_tables=%d",
        len(batches),
        sorted({batch.document_id for batch in batches}),
        len(results),
        [result.total for result in results],
        len(relevant),
    )
    channel = _table_channel(results, tables, relevant)
    rendered = channel.blocks
    table_tokens = sum(count_tokens(block) for block in rendered)
    passages = await render_text(question, batches, max(SYNTH_BUDGET - table_tokens, 0))
    evidence_files = _evidence_files(passages, rendered)
    _log_evidence(question, started, batches, passages, results, tables, channel)
    parts: list[str] = []
    async for token in synthesize(question, passages, rendered):
        parts.append(token)
        yield token
    _check_citations("".join(parts), evidence_files)
