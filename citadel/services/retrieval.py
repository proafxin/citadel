import asyncio
import functools
import logging
import operator
import time
from dataclasses import dataclass

import torch
from sqlalchemy import ColumnElement, bindparam, case, or_, select, update

from citadel.db import get_sessionmaker
from citadel.models.content import ContentNode
from citadel.models.document import Document
from citadel.models.library import Library
from citadel.models.status import DocumentStatus
from citadel.models.table import Table
from citadel.services.ingestion import get_redis
from config import get_embedder

logger = logging.getLogger(__name__)

EMBED_TTL = 86_400

RRF_K = 60
CANDIDATES = 1000
RETRIEVAL_CONCURRENCY = 8  # cap concurrent dense/sparse searches so a many-variant query can't exhaust the DB pool

VRAM_HEADROOM = 0.7  # fraction of free VRAM to spend on one embedding batch
BYTES_PER_TOKEN = 220_000  # BGE-M3 peak activation per token (~64MB at ~300 tok); calibrate with a benchmark
MODEL_MAX_TOKENS = 8192  # BGE-M3 context ceiling — caps the per-row cost estimate for very long nodes
BATCH_MAX = 256
EMBED_BATCH = 512  # nodes per read→embed→write cycle: bounds resident memory and keeps each write transaction short

_EMBED_LOCK = asyncio.Lock()


def _max_tokens(texts: list[str]) -> int:
    # true token length via the model's own tokenizer, not a char/4 proxy. only the longest-by-chars candidates can hold
    # the longest-by-tokens sequence, so tokenizing that handful is enough to size the batch without a full pass
    tokenizer = get_embedder().tokenizer
    candidates = sorted(texts, key=len, reverse=True)[:16]
    encoded = tokenizer(candidates, add_special_tokens=True)["input_ids"]
    return max((len(ids) for ids in encoded), default=1)


def _batch_size(texts: list[str]) -> int:
    # size one batch to the FREE VRAM and the longest text in the set (activation scales with batch x seq len),
    # flooring at 1 so a near-full GPU shrinks the batch instead of OOM-ing on a fixed minimum
    free, _ = torch.cuda.mem_get_info()
    longest = min(MODEL_MAX_TOKENS, _max_tokens(texts))
    return max(1, min(BATCH_MAX, int(free * VRAM_HEADROOM / (longest * BYTES_PER_TOKEN))))


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    vectors = get_embedder().encode(
        texts, batch_size=_batch_size(texts), normalize_embeddings=True, show_progress_bar=False
    )
    return [vector.tolist() for vector in vectors]


async def _embed(texts: list[str]) -> list[list[float]]:
    async with _EMBED_LOCK:
        return await asyncio.to_thread(embed_texts, texts)


@dataclass
class TableCand:
    content_id: str
    table_id: int
    filename: str
    n_rows: int
    columns: list[dict]
    description: str
    metadata: dict
    sample_rows: list[list]


TEXT_CHANNEL = ContentNode.type != "table"
TABLE_CHANNEL = ContentNode.type == "table"


@dataclass
class Retrieval:
    text: list[str]
    tables: list[str]


def _terms(queries: list[str]) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for query in queries:
        for token in query.split():
            key = token.casefold()
            if key and key not in seen:
                seen.add(key)
                terms.append(token)
    return terms


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@functools.lru_cache
def _search_semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(RETRIEVAL_CONCURRENCY)


async def _dense(channel: ColumnElement[bool], vector: list[float], library_id: int) -> list[str]:
    stmt = (
        select(ContentNode.content_id)
        .join(Document, ContentNode.document_id == Document.id)
        .where(channel, Document.library_id == library_id, ContentNode.embedding.isnot(None))
        .order_by(ContentNode.embedding.cosine_distance(vector))
        .limit(CANDIDATES)
    )
    async with _search_semaphore(), get_sessionmaker()() as session:
        return list(await session.scalars(stmt))


async def _sparse(channel: ColumnElement[bool], terms: list[str], library_id: int) -> list[str]:
    if not terms:
        return []
    conditions = [ContentNode.search_text.ilike(_like(term), escape="\\") for term in terms]
    hits = functools.reduce(operator.add, (case((cond, 1), else_=0) for cond in conditions))
    stmt = (
        select(ContentNode.content_id)
        .join(Document, ContentNode.document_id == Document.id)
        .where(channel, Document.library_id == library_id, ContentNode.search_text.isnot(None), or_(*conditions))
        .order_by(hits.desc())
        .limit(CANDIDATES)
    )
    async with _search_semaphore(), get_sessionmaker()() as session:
        return list(await session.scalars(stmt))


def _rrf(rankings: list[list[str]]) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, content_id in enumerate(ranking, start=1):
            scores[content_id] = scores.get(content_id, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores, key=lambda content_id: scores[content_id], reverse=True)


async def _channel(
    channel: ColumnElement[bool], vectors: list[list[float]], terms: list[str], library_id: int
) -> list[str]:
    # every dense (one per query variant) and the sparse search run concurrently, each on its own session, bounded by
    # the shared search semaphore — one question fans out into a single concurrent batch instead of serial round-trips
    searches = [_dense(channel, vector, library_id) for vector in vectors]
    searches.append(_sparse(channel, terms, library_id))
    return _rrf(await asyncio.gather(*searches))


async def _pending_node_ids(library_id: int) -> list[int]:
    async with get_sessionmaker()() as session:
        return list(
            await session.scalars(
                select(ContentNode.id)
                .join(Document, ContentNode.document_id == Document.id)
                .where(
                    Document.library_id == library_id,
                    ContentNode.search_text.isnot(None),
                    ContentNode.embedding.is_(None),
                )
            )
        )


async def _embed_batch(node_ids: list[int]) -> int:
    async with get_sessionmaker()() as session:
        rows = list(
            await session.execute(select(ContentNode.id, ContentNode.search_text).where(ContentNode.id.in_(node_ids)))
        )
    if not rows:
        return 0
    vectors = await _embed([search_text or "" for _id, search_text in rows])  # GPU work outside any open transaction
    params = [{"node_id": node_id, "emb": vector} for (node_id, _text), vector in zip(rows, vectors, strict=True)]
    stmt = update(ContentNode).where(ContentNode.id == bindparam("node_id")).values(embedding=bindparam("emb"))
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(stmt, params)
    return len(rows)


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
    node_ids = await _pending_node_ids(library_id)
    total = len(node_ids)
    logger.info("embed library=%d nodes=%d batches=%d", library_id, total, -(-total // EMBED_BATCH))
    embedded = 0
    started = time.time()
    for start in range(0, total, EMBED_BATCH):
        embedded += await _embed_batch(node_ids[start : start + EMBED_BATCH])
        elapsed = time.time() - started
        rate = embedded / elapsed if elapsed > 0 else 0.0
        await redis.hset(f"embed:{library_id}", mapping={"done": embedded, "total": total})
        logger.info("embed library=%d %d/%d nodes %.1fs %.0f nodes/s", library_id, embedded, total, elapsed, rate)
    await _mark_documents_embedded(library_id)
    await redis.hset(f"embed:{library_id}", mapping={"t_done": time.time(), "nodes": embedded})
    return embedded


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


async def retrieve(queries: list[str], library_id: int) -> Retrieval:
    vectors = await _embed(queries)
    terms = _terms(queries)
    text, tables = await asyncio.gather(
        _channel(TEXT_CHANNEL, vectors, terms, library_id),
        _channel(TABLE_CHANNEL, vectors, terms, library_id),
    )
    return Retrieval(text=text[:CANDIDATES], tables=tables[:CANDIDATES])


async def load_passages(content_ids: list[str]) -> list[str]:
    if not content_ids:
        return []
    async with get_sessionmaker()() as session:
        rows = list(
            await session.execute(
                select(ContentNode.content_id, Document.filename, ContentNode.page_no, ContentNode.search_text)
                .join(Document, ContentNode.document_id == Document.id)
                .where(ContentNode.content_id.in_(content_ids))
            )
        )
    lookup = {row.content_id: row for row in rows}
    passages: list[str] = []
    for content_id in content_ids:
        row = lookup.get(content_id)
        if row is not None and row.search_text:
            page = f" p{row.page_no}" if row.page_no else ""
            passages.append(f"[{row.filename}{page}] {row.search_text}")
    return passages


async def load_tables(content_ids: list[str]) -> list[TableCand]:
    if not content_ids:
        return []
    async with get_sessionmaker()() as session:
        rows = list(
            await session.execute(
                select(Table, Document.filename)
                .join(Document, Table.document_id == Document.id)
                .where(Table.content_id.in_(content_ids))
            )
        )
    lookup = {table.content_id: (table, filename) for table, filename in rows}
    out: list[TableCand] = []
    for content_id in content_ids:
        found = lookup.get(content_id)
        if found is not None:
            table, filename = found
            out.append(
                TableCand(
                    content_id,
                    table.id,
                    filename,
                    table.n_rows,
                    table.columns,
                    table.description,
                    table.table_metadata,
                    table.sample_rows,
                )
            )
    return out
