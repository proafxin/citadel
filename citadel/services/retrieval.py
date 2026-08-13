import asyncio
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import starmap

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from citadel.bus import get_redis
from citadel.db import get_sessionmaker
from citadel.llm import collect_embed, emit_embed
from citadel.models.batch import ContentBatch
from citadel.models.content import ContentNode
from citadel.models.document import Document
from citadel.models.embedding import Embedding
from citadel.models.library import Library
from citadel.models.status import DocumentStatus
from citadel.models.table import Table
from citadel.services.batching import render_block
from citadel.services.capacity import get_embed_capacity
from citadel.services.readiness import record_library_flag
from config import EMBED_MAX_TOKENS, get_embed_tokenizer

logger = logging.getLogger(__name__)

STREAM_EMBED = "embed_job"
EMBED_TTL = 86_400

UPSERT_COLS = 4
UPSERT_CHUNK = 32767 // UPSERT_COLS


async def _embed(text: str, key: str) -> list[float]:
    cap = get_embed_capacity()
    await cap.acquire(key)
    try:
        vectors = await collect_embed(await emit_embed([text], key))
    finally:
        await cap.release(key)
    return vectors[0]


@dataclass
class TableCand:
    content_id: int
    table_id: int
    document_id: int
    filename: str
    n_rows: int
    columns: list[dict]
    metadata: dict
    sample_rows: list[list]
    header_rows: list[int]
    page_no: int | None


@dataclass
class _PendingNode:
    content_id: int
    search_text: str
    node_type: str


async def _pending_nodes(library_id: int) -> list[_PendingNode]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(ContentNode.id, ContentNode.search_text, ContentNode.type)
            .join(Document, ContentNode.document_id == Document.id)
            .outerjoin(Embedding, Embedding.content_id == ContentNode.id)
            .where(
                Document.library_id == library_id,
                ContentNode.search_text.isnot(None),
                Embedding.content_id.is_(None),
            )
        )
    return [_PendingNode(row.id, row.search_text or "", row.type) for row in rows]


def _upsert_chunks(records: list[dict[str, object]], size: int) -> Iterator[list[dict[str, object]]]:
    for start in range(0, len(records), size):
        yield records[start : start + size]


def _truncate_for_embed(text: str) -> str:
    tokenizer = get_embed_tokenizer()
    ids = tokenizer.encode(text, add_special_tokens=True).ids
    if len(ids) < EMBED_MAX_TOKENS:
        return text
    return tokenizer.decode(ids, skip_special_tokens=True)


async def _embed_node(library_id: int, node: _PendingNode) -> dict[str, object]:
    key = f"embed:{library_id}:{node.content_id}"
    vector = await _embed(_truncate_for_embed(node.search_text), key)
    return {"content_id": node.content_id, "library_id": library_id, "type": node.node_type, "embedding": vector}


async def _write_records(records: list[dict[str, object]], library_id: int) -> None:
    write_t = time.time()
    async with get_sessionmaker()() as session, session.begin():
        for chunk in _upsert_chunks(records, UPSERT_CHUNK):
            ins = pg_insert(Embedding).values(chunk)
            await session.execute(
                ins.on_conflict_do_update(index_elements=["content_id"], set_={"embedding": ins.excluded.embedding})
            )
    logger.debug("embed library=%d write rows=%d %.2fs", library_id, len(records), time.time() - write_t)


async def _mark_documents_embedded(library_id: int) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            update(Document)
            .where(Document.library_id == library_id, Document.status == DocumentStatus.INGESTED)
            .values(status=DocumentStatus.EMBEDDED)
        )


async def embed_library(library_id: int) -> int:
    redis = get_redis()
    await redis.hset(f"embed:{library_id}", "t_start", time.time())
    await redis.expire(f"embed:{library_id}", EMBED_TTL)
    pending = await _pending_nodes(library_id)
    total = len(pending)
    logger.info("embed library=%d nodes=%d", library_id, total)
    started = time.time()
    embedded = 0
    buffer: list[dict[str, object]] = []
    jobs = [_embed_node(library_id, node) for node in pending]
    for coro in asyncio.as_completed(jobs):
        buffer.append(await coro)
        embedded += 1
        if len(buffer) >= UPSERT_CHUNK:
            await _write_records(buffer, library_id)
            buffer = []
        await redis.hset(f"embed:{library_id}", mapping={"done": embedded, "total": total})
    if buffer:
        await _write_records(buffer, library_id)
    elapsed = time.time() - started
    rate = embedded / elapsed if elapsed > 0 else 0.0
    logger.info("embed library=%d done nodes=%d %.1fs %.0f nodes/s", library_id, embedded, elapsed, rate)
    await _mark_documents_embedded(library_id)
    await redis.hset(f"embed:{library_id}", mapping={"t_done": time.time(), "nodes": embedded})
    return embedded


async def handle_embed(fields: dict[str, str]) -> None:
    library_id = int(fields["library_id"])
    await embed_library(library_id)
    await record_library_flag(library_id, "embedding")


async def pending_libraries() -> list[int]:
    async with get_sessionmaker()() as session:
        return list(
            await session.scalars(
                select(Document.library_id)
                .join(Library, Document.library_id == Library.id)
                .where(Library.tier == "tier_2", Document.status == DocumentStatus.INGESTED)
                .distinct()
            )
        )


def _table_cand(table: Table, filename: str, page_no: int | None) -> TableCand:
    return TableCand(
        table.content_id,
        table.id,
        table.document_id,
        filename,
        table.n_rows,
        table.columns,
        table.table_metadata,
        table.sample_rows,
        (table.anchors or {}).get("header_rows", []),
        page_no,
    )


async def load_all_tables(library_id: int) -> list[TableCand]:
    async with get_sessionmaker()() as session:
        rows = list(
            await session.execute(
                select(Table, Document.filename, ContentNode.page_no)
                .join(Document, Table.document_id == Document.id)
                .join(ContentNode, Table.content_id == ContentNode.id)
                .where(Document.library_id == library_id)
                .order_by(Table.id)
            )
        )
    return list(starmap(_table_cand, rows))


async def scope_block_ids(ranges: list[tuple[int, int, int]]) -> list[int]:
    if not ranges:
        return []
    clauses = [
        (ContentNode.document_id == doc_id) & (ContentNode.block_ordinal >= start) & (ContentNode.block_ordinal <= end)
        for doc_id, start, end in ranges
    ]
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(ContentNode.id).where(or_(*clauses)).order_by(ContentNode.id)))


@dataclass
class BlockText:
    content_id: int
    document_id: int
    block_ordinal: int
    text: str


async def load_block_texts(content_ids: list[int]) -> dict[int, BlockText]:
    if not content_ids:
        return {}
    async with get_sessionmaker()() as session:
        nodes = list(
            await session.execute(
                select(ContentNode, Document.filename)
                .join(Document, ContentNode.document_id == Document.id)
                .where(ContentNode.id.in_(content_ids))
            )
        )
        table_ids = [node.id for node, _ in nodes if node.type == "table"]
        tables = (
            {t.content_id: t for t in await session.scalars(select(Table).where(Table.content_id.in_(table_ids)))}
            if table_ids
            else {}
        )
    out: dict[int, BlockText] = {}
    for node, filename in nodes:
        body = render_block(node, tables.get(node.id))
        if body.strip():
            page = f" p{node.page_no}" if node.page_no else ""
            out[node.id] = BlockText(node.id, node.document_id, node.block_ordinal or 0, f"[{filename}{page}] {body}")
    return out


@dataclass
class BatchRef:
    id: int
    document_id: int
    filename: str
    summary: str
    summary_tokens: int
    start_block_ordinal: int
    end_block_ordinal: int
    start_page_no: int | None
    end_page_no: int | None
    content_tokens: int


@dataclass
class DocRef:
    id: int
    filename: str
    summary: str
    content_tokens: int


async def load_library_documents(library_id: int) -> list[DocRef]:
    async with get_sessionmaker()() as session:
        rows = list(
            await session.execute(
                select(
                    Document.id,
                    Document.filename,
                    Document.summary,
                    func.coalesce(func.sum(ContentBatch.content_tokens), 0),
                )
                .join(ContentBatch, ContentBatch.document_id == Document.id)
                .where(Document.library_id == library_id, Document.summary.is_not(None))
                .group_by(Document.id, Document.filename, Document.summary)
                .order_by(Document.id)
            )
        )
    return [DocRef(doc_id, filename, summary, int(tokens)) for doc_id, filename, summary, tokens in rows]


async def load_library_name(library_id: int) -> str:
    async with get_sessionmaker()() as session:
        return await session.scalar(select(Library.name).where(Library.id == library_id)) or ""


async def load_library_batches(library_id: int) -> list[BatchRef]:
    async with get_sessionmaker()() as session:
        rows = list(
            await session.execute(
                select(ContentBatch, Document.filename)
                .join(Document, ContentBatch.document_id == Document.id)
                .where(Document.library_id == library_id)
                .order_by(ContentBatch.document_id, ContentBatch.batch_no)
            )
        )
    return [
        BatchRef(
            batch.id,
            batch.document_id,
            filename,
            batch.summary,
            batch.summary_tokens,
            batch.start_block_ordinal,
            batch.end_block_ordinal,
            batch.start_page_no,
            batch.end_page_no,
            batch.content_tokens,
        )
        for batch, filename in rows
    ]
