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
    count_tokens,
    count_tokens_batch,
    merge_evidence,
    resolve_query,
    synthesize,
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
    scope_block_ids,
)

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 3000

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


def _cte(tables: list[TableCand]) -> str:
    return "WITH " + ", ".join(starmap(_view_cte, enumerate(tables)))


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


def _row_file_sources(columns: list[str], rows: list[list]) -> list[str]:
    # a query that names no tN view has no refs to source from — but when its RESULT rows carry a `file` (and, when
    # present, `sheet`), those are the real subject of the answer ("the largest is sales_test.csv"), so the label is
    # built from them rather than left as an anonymous "computed result"
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


def _result_sources(tables: list[TableCand], refs: list[int], columns: list[str], rows: list[list]) -> list[str]:
    # what the result traces back to. a tN query sources from the tables it read. a query that reads no tN takes its
    # origin from the files its rows name, or — when it names none — every file this request was built from. a computed
    # figure is never left origin-less
    if refs:
        return _sources(tables, refs)
    return _row_file_sources(columns, rows) or _all_files(tables)


async def _run_sql(session: AsyncSession, tables: list[TableCand], sql: str) -> tuple[list[str], list[list]]:
    cte = _cte(tables)
    await session.execute(text("SET TRANSACTION READ ONLY"))
    await session.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(STATEMENT_TIMEOUT_MS)})
    connection = await session.connection()
    result = await connection.exec_driver_sql(f"{cte} {sql}")
    return list(result.keys()), [list(row) for row in result.fetchall()]


async def _execute(tables: list[TableCand], sql: str) -> SqlResult | None:
    sql = sql.strip().rstrip(";").strip()
    try:
        async with get_sessionmaker()() as session, session.begin():
            columns, rows = await _run_sql(session, tables, sql)
    except SQLAlchemyError:
        logger.warning("sql failed sql=%s\n%s", sql, traceback.format_exc())
        return None
    refs = _refs(tables, sql)
    sources = _result_sources(tables, refs, columns, rows)
    logger.info("resolve sources=%s rows=%d sql=%s", sources, len(rows), sql)
    return SqlResult(
        ", ".join(sources) or "computed result", columns, rows, len(rows), refs, _readable_sql(tables, sql)
    )


async def _aggregate(question: str, tables: list[TableCand], library: str = "") -> list[SqlResult]:
    blocks = list(starmap(_schema_block, enumerate(tables)))
    plan = await write_queries(question, blocks, library) if tables else QueryPlan()
    results: list[SqlResult] = []
    for sql in plan.queries:
        resolved = await _execute(tables, sql)
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
    # every covered table is described — that is the floor, and it is laid in FIRST, so a table can never lose its place
    # in the answer to another table's rows. computed rows are then fitted into what the whole floor left, both this
    # channel's and the text channel's: rows are an answer, and an answer may not crowd out what must be accounted for
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


def _doc_item(doc: DocRef) -> str:
    return f"document — {doc.filename}: {doc.summary}"


def _table_item(table: TableCand) -> str:
    # a table's identity: where it sits, what it is called, how many rows it holds, what its columns are named. no
    # dtypes and no sample values — those are the width-scaled part, bought later for the few tables that earn them.
    # the row count stays: it is one number we hold, and how big a table is is itself something a question can be about
    locator = _table_locator(table)
    named = [str(table.metadata[key]) for key in ("title", "caption") if table.metadata.get(key)]
    columns = ", ".join(str(column.get("header") or "?") for column in table.columns)
    head = f"table — {table.filename}{f' ({locator})' if locator else ''} rows={table.n_rows}"
    return f"{head}: {' | '.join(named)}. columns {columns}" if named else f"{head}: columns {columns}"


@dataclass
class _Coverage:
    documents: dict[int, str]  # index into the library's documents -> depth
    tables: dict[int, str]  # index into the library's tables -> depth


def _split_coverage(coverage: dict[int, str], n_docs: int) -> _Coverage:
    # the inventory is documents then tables in one numbering, so an item's kind is decided by where its number falls.
    # `parts` is a document depth; a table named there asked for detail inside it, which for a table means its rows
    documents = {index: depth for index, depth in coverage.items() if index < n_docs}
    tables: dict[int, str] = {}
    for index, depth in coverage.items():
        if index >= n_docs:
            if depth == "parts":
                logger.warning("resolve named a table at parts item=%d — taking its rows", index)
            tables[index - n_docs] = "full" if depth == "parts" else depth
    return _Coverage(documents, tables)


async def resolve(question: str, documents: list[DocRef], tables: list[TableCand], library: str) -> _Coverage:
    items = [_doc_item(doc) for doc in documents] + [_table_item(table) for table in tables]
    if not items:
        return _Coverage({}, {})
    return _split_coverage(await resolve_query(question, items, library), len(documents))


async def run_tables(question: str, tables: list[TableCand], depths: dict[int, str], library: str) -> list[SqlResult]:
    # the table channel now runs over the tables the question was resolved as needing values FROM, and only those: they
    # arrive with full schema and samples, which is the view worth paying for once the set is small
    queried = [tables[index] for index in sorted(depths) if depths[index] == "full"]
    if not queried:
        return []
    return await _aggregate(question, queried, library)


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


def _doc_summary_text(doc: DocRef) -> str:
    return f"[{doc.filename}] {doc.summary}"


def _doc_evidence(doc: DocRef, tokens: int) -> _Evidence:
    return _Evidence(_doc_summary_text(doc), [doc.id], tokens, 0, None, doc.id)


async def _reduce_floor(question: str, documents: list[DocRef], counts: list[int], budget: int) -> list[str]:
    # the floor itself does not fit: the covered documents' own summaries are already the shortest form each has, so
    # what is left is to merge them — never to drop one. `_reduce` merges within a document first and only widens the
    # grain when that did not free enough, so a document leaves the answer as a whole only when nothing else remains
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
    # what a document reads as one rung below its summary: the summaries of its own parts, or their wording
    if depth == "parts":
        return [_summary_text(batch) for batch in batches]
    return await _doc_blocks(batches)


def _upgrade_order(documents: list[DocRef], depths: dict[int, str]) -> list[tuple[DocRef, str, bool]]:
    # `full` before `parts`: the deeper ask is the one the answer turns on, so it gets first claim on what is spare.
    # then, with whatever is STILL spare, every remaining covered document is carried one rung deeper than it asked
    # for — a summary is the cheapest form of a document, not the most faithful one, and an unspent budget buys nothing
    asked: list[tuple[DocRef, str, bool]] = [
        (doc, depth, False) for depth in ("full", "parts") for doc in documents if depths.get(doc.id) == depth
    ]
    return asked + [(doc, "parts", True) for doc in documents if depths.get(doc.id, "overall") == "overall"]


async def _upgrade(
    doc: DocRef, depth: str, batches: list[BatchRef], floor_cost: int, spare: int
) -> tuple[list[str], int] | None:
    texts = await _deeper_texts(depth, batches)
    if not texts:
        return None
    cost = sum(await asyncio.to_thread(count_tokens_batch, texts))
    return (texts, cost - floor_cost) if cost - floor_cost <= spare else None


async def render_text(
    question: str, documents: list[DocRef], depths: dict[int, str], batches: dict[int, list[BatchRef]], budget: int
) -> list[str]:
    # every covered document is present at its summary before anything is deepened — that is the floor, and it is what
    # keeps coverage a property of the output rather than an outcome of the budget. whatever the floor leaves is then
    # spent deepening the documents that asked for depth, each one all-or-nothing against what is still spare
    if not documents or budget <= 0:
        logger.info("text floor documents=%d budget=%d", len(documents), budget)
        return []
    floor = [_doc_summary_text(doc) for doc in documents]
    counts = await asyncio.to_thread(count_tokens_batch, floor)
    if sum(counts) > budget:
        return await _reduce_floor(question, documents, counts, budget)
    spare = budget - sum(counts)
    rendered = {doc.id: [text] for doc, text in zip(documents, floor, strict=True)}
    costs = dict(zip((doc.id for doc in documents), counts, strict=True))
    deepened: dict[int, str] = {}
    greedy: list[str] = []
    for doc, depth, spent_spare in _upgrade_order(documents, depths):
        upgraded = await _upgrade(doc, depth, batches.get(doc.id, []), costs[doc.id], spare)
        if upgraded is not None:
            rendered[doc.id], delta = upgraded
            spare -= delta
            deepened[doc.id] = depth
            if spent_spare:
                greedy.append(doc.filename)
    logger.info(
        "text documents=%d floor=%d asked=%s greedy=%d spare=%d budget=%d",
        len(documents),
        sum(counts),
        {doc.filename: deepened[doc.id] for doc in documents if doc.id in deepened and doc.filename not in greedy},
        len(greedy),
        spare,
        budget,
    )
    return [text for doc in documents for text in rendered[doc.id]]


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
) -> _Assembled:
    # the floor of BOTH channels is priced before a single row is fitted: every covered table's description and every
    # covered document's summary. what is left over is what computed rows may take, and what they leave is depth
    describe = [_table_render(table) for table in tables]
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
    coverage = await resolve(question, documents, tables, library)
    covered_docs = [documents[index] for index in sorted(coverage.documents)]
    covered_tables = [tables[index] for index in sorted(coverage.tables)]
    depths = {documents[index].id: depth for index, depth in coverage.documents.items()}
    results = await run_tables(question, tables, coverage.tables, library)
    logger.info(
        "coverage documents=%d/%d tables=%d/%d results=%d rows=%s",
        len(covered_docs),
        len(documents),
        len(covered_tables),
        len(tables),
        len(results),
        [result.total for result in results],
    )
    built = await _assemble(question, library_id, covered_docs, covered_tables, depths, results)
    evidence_files = _evidence_files(built.passages, built.rendered)
    _log_evidence(question, started, built.passages, results, built.channel)
    parts: list[str] = []
    async for token in synthesize(question, built.passages, built.rendered):
        parts.append(token)
        yield token
    _check_citations("".join(parts), evidence_files)
