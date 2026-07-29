import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from citadel.bus import get_redis
from citadel.db import get_sessionmaker
from citadel.llm import collect_text, count_tokens, count_tokens_batch, emit_text
from citadel.models.batch import ContentBatch
from citadel.models.content import ContentNode
from citadel.models.document import Document
from citadel.models.status import DocumentStatus
from citadel.models.table import Table
from citadel.prompts import load_prompt

STREAM_BATCH = "batch"  # one job per MERGED DOCUMENT: packs it into batches and summarizes them all

logger = logging.getLogger(__name__)

BATCH_TOKENS = 32768
SAMPLE_ROWS = 3
SUMMARY_RATIO = 0.1
SUMMARY_TOKENS_MAX = 4096
# a document's entry in the inventory the query is resolved against. every document AND every table in the library must
# fit that one call, so this is a per-document ceiling, not a ratio of the document's length
DOCUMENT_SUMMARY_TOKENS = 500

_BLOCK_ORDINALS = text("""
UPDATE content SET block_ordinal = seq.rn
FROM (
    SELECT id, row_number() OVER (
        PARTITION BY document_id ORDER BY COALESCE(page_no, sheet_no), ordinal
    ) AS rn
    FROM content WHERE document_id = :doc_id
) AS seq
WHERE content.id = seq.id
""")


@dataclass
class BlockText:
    content_id: int
    block_ordinal: int
    page_no: int | None
    text: str
    tokens: int


@dataclass
class BatchSpec:
    document_id: int
    batch_no: int
    start_block_ordinal: int
    end_block_ordinal: int
    start_page_no: int | None
    end_page_no: int | None
    text: str
    content_tokens: int


async def assign_block_ordinals(session: AsyncSession, doc_id: int) -> None:
    await session.execute(_BLOCK_ORDINALS, {"doc_id": doc_id})


def _table_text(table: Table) -> str:
    # the row count leads, and the rows are declared a SAMPLE. without that a reader sees three rows and no statement of
    # size, and the only available reading is that the table HAS three rows — measured: answers claiming a 834-row sheet
    # "displays 3 rows". how big the table is cannot be inferred from an excerpt, so it is stated
    headers = [str(column.get("header") or "") for column in table.columns]
    shown = table.sample_rows[:SAMPLE_ROWS]
    lines = [f"table with {table.n_rows} rows, {len(shown)} shown as a sample:", " | ".join(headers)]
    lines.extend(" | ".join("" if value is None else str(value) for value in row) for row in shown)
    return "\n".join(lines)


def render_raw(raw: dict | None) -> str:
    # the block's structure is part of its meaning: an item's position answers "the second item", indentation carries
    # nesting, LaTeX needs its delimiters. search_text flattens all of that away, so it is for matching only — anything
    # a model has to READ (a summary's input, an answer's evidence) is rendered from raw
    fields = raw or {}
    if items := fields.get("items"):
        return "\n".join(
            "  " * int(item.get("depth", 0) or 0) + f"{int(item.get('ordinal', 0)) + 1}. {item.get('content', '')}"
            for item in items
        )
    if latex := fields.get("latex"):
        return f"$$\n{latex}\n$$"
    return str(fields.get("text") or "")


def render_block(node: ContentNode, table: Table | None) -> str:
    return _table_text(table) if table is not None else render_raw(node.raw)


async def load_blocks(session: AsyncSession, doc_id: int) -> list[BlockText]:
    nodes = list(
        await session.scalars(
            select(ContentNode).where(ContentNode.document_id == doc_id).order_by(ContentNode.block_ordinal)
        )
    )
    tables = {row.content_id: row for row in await session.scalars(select(Table).where(Table.document_id == doc_id))}
    kept = [(node, body) for node in nodes if (body := render_block(node, tables.get(node.id))).strip()]
    counts = count_tokens_batch([body for _, body in kept])
    return [
        BlockText(
            content_id=node.id,
            block_ordinal=node.block_ordinal or 0,
            page_no=node.page_no,
            text=body,
            tokens=tokens,
        )
        for (node, body), tokens in zip(kept, counts, strict=True)
    ]


def _spec(document_id: int, batch_no: int, blocks: list[BlockText]) -> BatchSpec:
    pages = [block.page_no for block in blocks if block.page_no is not None]
    return BatchSpec(
        document_id=document_id,
        batch_no=batch_no,
        start_block_ordinal=blocks[0].block_ordinal,
        end_block_ordinal=blocks[-1].block_ordinal,
        start_page_no=min(pages) if pages else None,
        end_page_no=max(pages) if pages else None,
        text="\n\n".join(block.text for block in blocks),
        content_tokens=sum(block.tokens for block in blocks),
    )


def pack_batches(document_id: int, blocks: list[BlockText]) -> list[BatchSpec]:
    specs: list[BatchSpec] = []
    current: list[BlockText] = []
    size = 0
    for block in blocks:
        if current and size + block.tokens > BATCH_TOKENS:
            specs.append(_spec(document_id, len(specs) + 1, current))
            current, size = [], 0
        current.append(block)
        size += block.tokens
    if current:
        specs.append(_spec(document_id, len(specs) + 1, current))
    return specs


def summary_budget(content_tokens: int) -> int:
    return min(int(content_tokens * SUMMARY_RATIO), SUMMARY_TOKENS_MAX)


async def store_token_counts(session: AsyncSession, blocks: list[BlockText]) -> None:
    if not blocks:
        return
    await session.execute(
        update(ContentNode),
        [{"id": block.content_id, "qwen_token_count": block.tokens} for block in blocks],
    )


async def build_document_specs(session: AsyncSession, doc_id: int) -> list[BatchSpec]:
    await assign_block_ordinals(session, doc_id)
    blocks = await load_blocks(session, doc_id)
    if not blocks:
        return []
    await store_token_counts(session, blocks)
    return pack_batches(doc_id, blocks)


_SUMMARY_LOCK = 2  # advisory-lock namespace for the per-library summary-completion check (disjoint from the embed lock)


def _summaries_done_stream(library_id: int) -> str:
    return f"summaries:done:{library_id}"


async def _pending_summaries(session: AsyncSession, library_id: int) -> int:
    # documents whose batches are not summarized yet. a FAILED document is excluded — it is not INGESTED/PARTIAL, so it
    # owes nothing and can never hold the library back; a redelivered job re-marks the same row without moving the count
    pending = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(
            Document.library_id == library_id,
            Document.status.in_((DocumentStatus.INGESTED, DocumentStatus.PARTIAL)),
            Document.summarized_at.is_(None),
        )
    )
    return int(pending or 0)


async def emit_library_batches(library_id: int) -> int:
    # summarizing is retrieval prep, not structure, so it runs as ONE post-ingestion pass over the whole library rather
    # than per document at merge: off the ocr-contended window, and never leaving the resolver a partial inventory. one
    # job per document lands on the same stream the worker already drains with bounded concurrency. the completion
    # stream is cleared BEFORE any job can fire, so a stale entry from a prior run is never mistaken for this one's
    async with get_sessionmaker()() as session:
        doc_ids = list(
            await session.scalars(
                select(Document.id).where(
                    Document.library_id == library_id,
                    Document.status.in_((DocumentStatus.INGESTED, DocumentStatus.PARTIAL)),
                )
            )
        )
    redis = get_redis()
    await redis.delete(_summaries_done_stream(library_id))
    for doc_id in doc_ids:
        await redis.xadd(STREAM_BATCH, {"doc_id": str(doc_id), "library_id": str(library_id)})
    return len(doc_ids)


async def record_summary(library_id: int) -> None:
    # counter-and-fire, DB-backed like embed's inflight check: once a document's summary commits, the job that finds no
    # summaries left fires the library's completion stream. the advisory lock serializes the check, so two documents
    # finishing together cannot both miss zero and leave the library never firing
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:cls, :lib)"), {"cls": _SUMMARY_LOCK, "lib": library_id}
        )
        remaining = await _pending_summaries(session, library_id)
    if remaining == 0:
        await get_redis().xadd(_summaries_done_stream(library_id), {"library_id": str(library_id)})


async def wait_library_summaries(library_id: int, emitted: int) -> None:
    # the last summary to land fires the completion stream; block on it instead of polling. reading from "0" is
    # race-free — the entry persists, so a fire that happened before this read is still seen
    if emitted == 0:
        return
    await get_redis().xread({_summaries_done_stream(library_id): "0"}, block=0)


def _summary_prompt(spec: BatchSpec) -> tuple[str, int]:
    budget = summary_budget(spec.content_tokens)
    prompt = f"{load_prompt('batch_summary').replace('{summary_tokens}', str(budget))}\ntext:\n{spec.text}"
    # the completion cap sits ABOVE the summary asked for — the ask is advisory (a model cannot count its own tokens)
    # while the cap is hard. the reply is plain text, so content that resists compression comes back SHORTER rather
    # than unparseable; a batch is never lost to its own length
    return prompt, budget * 2 + 512


def _document_summary_prompt(filename: str, summaries: list[str]) -> tuple[str, int]:
    body = "\n\n".join(summaries)
    prompt = (
        f"{load_prompt('document_summary').replace('{summary_tokens}', str(DOCUMENT_SUMMARY_TOKENS))}\n"
        f"filename: {filename}\nparts:\n{body}"
    )
    return prompt, DOCUMENT_SUMMARY_TOKENS * 2 + 512


async def _document_summary(doc_id: int, summaries: list[str]) -> str | None:
    # the document's own summary, reduced from the parts already summarized. it is written after them and from them
    # only — nothing re-reads the document — so it costs one call per document and cannot disagree with its parts
    if not summaries:
        return None
    async with get_sessionmaker()() as session:
        filename = await session.scalar(select(Document.filename).where(Document.id == doc_id)) or ""
    return await collect_text(await emit_text(*_document_summary_prompt(filename, summaries), interactive=False))


def _batch_row(doc_id: int, spec: BatchSpec, summary: str) -> ContentBatch:
    return ContentBatch(
        document_id=doc_id,
        batch_no=spec.batch_no,
        start_block_ordinal=spec.start_block_ordinal,
        end_block_ordinal=spec.end_block_ordinal,
        start_page_no=spec.start_page_no,
        end_page_no=spec.end_page_no,
        summary=summary,
        summary_tokens=count_tokens(summary),
        content_tokens=spec.content_tokens,
    )


async def summarize_document(fields: dict[str, str]) -> None:
    # ONE job per merged document: pack it into batches and summarize all of them. every batch's summary is emitted to
    # the slm stream first and collected after, so a document's batches are summarized concurrently rather than one
    # after another — and many documents are in flight at once, since the stage itself is unbounded.
    # the batches and the document's mark are written in ONE transaction: a document is never left half-summarized, and
    # re-running the job simply replaces the same rows
    doc_id = int(fields["doc_id"])
    started = time.time()
    async with get_sessionmaker()() as session, session.begin():
        specs = await build_document_specs(session, doc_id)
    packed = time.time()
    jobs = [(spec, await emit_text(*_summary_prompt(spec), interactive=False)) for spec in specs]
    rows = [_batch_row(doc_id, spec, await collect_text(job_id)) for spec, job_id in jobs]
    summary = await _document_summary(doc_id, [row.summary for row in rows])
    summarized = time.time()
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(delete(ContentBatch).where(ContentBatch.document_id == doc_id))
        session.add_all(rows)
        await session.execute(
            update(Document).where(Document.id == doc_id).values(summary=summary, summarized_at=datetime.now(UTC))
        )
    # pack vs summarize is the split that matters: packing is ours (CPU, tokenizing), summarizing is the model's. how
    # far this span runs against the rest of ingestion says whether summaries finish inside it or tail past it
    logger.info(
        "batches doc=%d batches=%d content=%d pack=%.1fs summarize=%.1fs total=%.1fs",
        doc_id,
        len(rows),
        sum(spec.content_tokens for spec in specs),
        packed - started,
        summarized - packed,
        time.time() - started,
    )
