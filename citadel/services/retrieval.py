import asyncio
import functools
import operator
from dataclasses import dataclass

import torch
from sqlalchemy import ColumnElement, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from citadel.db import get_sessionmaker
from citadel.models.content import ContentNode
from citadel.models.document import Document
from citadel.models.table import Table
from config import get_embedder

RRF_K = 60
CANDIDATES = 1000

VRAM_HEADROOM = 0.7  # fraction of free VRAM to spend on one embedding batch
BYTES_PER_ROW = 40_000_000  # BGE-M3 activation per row at typical search_text length; tune with a benchmark
BATCH_MIN = 8
BATCH_MAX = 256

_EMBED_LOCK = asyncio.Lock()


def _batch_size() -> int:
    if not torch.cuda.is_available():
        return BATCH_MIN
    free, _ = torch.cuda.mem_get_info()
    return max(BATCH_MIN, min(BATCH_MAX, int(free * VRAM_HEADROOM / BYTES_PER_ROW)))


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    vectors = get_embedder().encode(texts, batch_size=_batch_size(), normalize_embeddings=True, show_progress_bar=False)
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


async def _dense(session: AsyncSession, channel: ColumnElement[bool], vector: list[float]) -> list[str]:
    stmt = (
        select(ContentNode.content_id)
        .where(channel, ContentNode.embedding.isnot(None))
        .order_by(ContentNode.embedding.cosine_distance(vector))
        .limit(CANDIDATES)
    )
    return list(await session.scalars(stmt))


async def _sparse(session: AsyncSession, channel: ColumnElement[bool], terms: list[str]) -> list[str]:
    if not terms:
        return []
    conditions = [ContentNode.search_text.ilike(_like(term), escape="\\") for term in terms]
    hits = functools.reduce(operator.add, (case((cond, 1), else_=0) for cond in conditions))
    stmt = (
        select(ContentNode.content_id)
        .where(channel, ContentNode.search_text.isnot(None), or_(*conditions))
        .order_by(hits.desc())
        .limit(CANDIDATES)
    )
    return list(await session.scalars(stmt))


def _rrf(rankings: list[list[str]]) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, content_id in enumerate(ranking, start=1):
            scores[content_id] = scores.get(content_id, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores, key=lambda content_id: scores[content_id], reverse=True)


async def _channel(
    session: AsyncSession, channel: ColumnElement[bool], vectors: list[list[float]], terms: list[str]
) -> list[str]:
    lists = [await _dense(session, channel, vector) for vector in vectors]
    lists.append(await _sparse(session, channel, terms))
    return _rrf(lists)


async def _embed_nodes(session: AsyncSession, nodes: list[ContentNode]) -> int:
    if not nodes:
        return 0
    vectors = await _embed([node.search_text or "" for node in nodes])
    for node, vector in zip(nodes, vectors, strict=True):
        node.embedding = vector
    return len(nodes)


async def embed_library(library_id: int) -> int:
    async with get_sessionmaker()() as session, session.begin():
        nodes = list(
            await session.scalars(
                select(ContentNode)
                .join(Document, ContentNode.document_id == Document.id)
                .where(
                    Document.library_id == library_id,
                    ContentNode.search_text.isnot(None),
                    ContentNode.embedding.is_(None),
                )
            )
        )
        return await _embed_nodes(session, nodes)


async def embed_all_pending() -> int:
    async with get_sessionmaker()() as session, session.begin():
        nodes = list(
            await session.scalars(
                select(ContentNode).where(ContentNode.search_text.isnot(None), ContentNode.embedding.is_(None))
            )
        )
        return await _embed_nodes(session, nodes)


async def retrieve(queries: list[str]) -> Retrieval:
    vectors = await _embed(queries)
    terms = _terms(queries)
    async with get_sessionmaker()() as session:
        text = await _channel(session, TEXT_CHANNEL, vectors, terms)
        tables = await _channel(session, TABLE_CHANNEL, vectors, terms)
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
