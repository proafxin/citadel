import asyncio
import functools
import logging
import operator
import time
from collections.abc import Iterator
from dataclasses import dataclass

import torch
from sqlalchemy import case, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from citadel.bus import get_redis
from citadel.db import get_sessionmaker
from citadel.models.content import ContentNode
from citadel.models.document import Document
from citadel.models.embedding import Embedding
from citadel.models.library import Library
from citadel.models.status import DocumentStatus
from citadel.models.table import Table
from config import get_embedder

logger = logging.getLogger(__name__)

EMBED_TTL = 86_400

RRF_K = 60
CANDIDATES = 1000
RETRIEVAL_CONCURRENCY = 8  # cap concurrent dense/sparse searches so a many-variant query can't exhaust the DB pool

EMBED_VRAM_HEADROOM = 0.7  # of what is actually free — the floor, so a genuinely full card still yields a safe batch
EMBED_VRAM_CAP = 2 * 1024**3  # ...but never more than this, whatever is lying around. the batch used to be sized on
# free VRAM ALONE, which made this process's memory a function of everyone else's: shrink a co-located model's
# reservation and the embedder helps itself to the difference, takes a bigger batch, and OOMs — the one time we freed
# VRAM on purpose is the time it broke. capping it bounds this process no matter how empty the card looks, and leaves
# the rest genuinely spendable on the models that need it. it costs only embedding wall time, a one-off pass that is
# never the bottleneck
MODEL_MAX_TOKENS = 8192
BYTES_PER_TOKEN = 21_750  # activations: linear in tokens, so batch x longest bounds them
MASK_BYTES_PER_TOKEN = 2  # the attention mask is NOT linear. transformers materializes it as (batch, 1, L, L) to hand
# to sdpa, in the model's bf16 — one element per token PER key — so it costs 2 x longest bytes per token, i.e. it grows
# with the SQUARE of sequence length. at longest=917 that is 2KB a token against 21.75KB of activations and it hides
# inside the linear fit; at longest=8192 it is 16KB a token and dominates. sizing a batch on tokens alone therefore
# reads the same for a group of short nodes and a group of long ones, and OOMs on the long one: 31 x 8192^2 x 2 =
# 3.88GiB, which is exactly the allocation that failed
UPSERT_COLS = 4
UPSERT_CHUNK = 32767 // UPSERT_COLS

_EMBED_LOCK = asyncio.Lock()


def _max_tokens(texts: list[str]) -> int:
    # true token length via the model's own tokenizer; only the longest-by-chars candidates hold the longest-by-tokens
    tokenizer = get_embedder().tokenizer
    candidates = sorted(texts, key=len, reverse=True)[:16]
    encoded = tokenizer(candidates, add_special_tokens=True)["input_ids"]
    return min(MODEL_MAX_TOKENS, max((len(ids) for ids in encoded), default=1))


def available_vram() -> int:
    driver_free, _ = torch.cuda.mem_get_info()
    cached_free = torch.cuda.memory_reserved() - torch.cuda.memory_allocated()
    return driver_free + cached_free


def bytes_per_token(longest: int) -> int:
    # what ONE padded token of a batch actually costs: its activations, plus its row of the (L x L) attention mask
    return BYTES_PER_TOKEN + MASK_BYTES_PER_TOKEN * longest


def vram_budget() -> int:
    # the smaller of what is free and what we are willing to spend. free-VRAM alone is what OOM'd this (EMBED_VRAM_CAP)
    return min(int(EMBED_VRAM_HEADROOM * available_vram()), EMBED_VRAM_CAP)


def token_budget() -> int:
    # how many tokens a group may pack. priced at the model's longest possible node, since a group is packed before its
    # longest is known and a single long node would otherwise blow the batch it lands in
    return max(MODEL_MAX_TOKENS, vram_budget() // bytes_per_token(MODEL_MAX_TOKENS))


def embed_texts(texts: list[str], longest: int) -> list[list[float]]:
    if not texts:
        return []
    budget = vram_budget()
    batch_size = max(1, budget // (max(longest, 1) * bytes_per_token(longest)))
    torch.cuda.reset_peak_memory_stats()
    baseline = torch.cuda.memory_allocated()
    vectors = get_embedder().encode(texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False)
    activation = torch.cuda.max_memory_allocated() - baseline
    logger.info(
        "embed_encode rows=%d longest=%d batch=%d per_token=%d budget=%dMiB act=%dMiB avail=%dMiB",
        len(texts),
        longest,
        batch_size,
        bytes_per_token(longest),
        budget >> 20,
        activation >> 20,
        available_vram() >> 20,
    )
    return [vector.tolist() for vector in vectors]


async def _embed(texts: list[str], longest: int) -> list[list[float]]:
    async with _EMBED_LOCK:
        return await asyncio.to_thread(embed_texts, texts, longest)


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
    header_rows: list[int]  # row_idx values in table_rows that are header, not data — the query projection skips them


@dataclass
class Retrieval:
    text: list[str]
    tables: list[str]


@dataclass
class Passage:
    content_id: str
    text: str
    document_id: int
    section_id: int | None
    score: int = 0


@dataclass
class _PendingNode:
    content_id: str
    search_text: str
    node_type: str
    token_len: int = 0


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


async def _dense(is_table: bool, vector: list[float], library_id: int) -> list[str]:
    # dense search runs entirely on the small embeddings table: library_id is denormalized there, so retrieval is
    # library-scoped (no cross-library leak) with no join, and the HNSW/halfvec index does the ANN ordering
    channel = (Embedding.type == "table") if is_table else (Embedding.type != "table")
    stmt = (
        select(Embedding.content_id)
        .where(Embedding.library_id == library_id, channel)
        .order_by(Embedding.embedding.cosine_distance(vector))
        .limit(CANDIDATES)
    )
    async with _search_semaphore(), get_sessionmaker()() as session:
        return list(await session.scalars(stmt))


async def _sparse(is_table: bool, terms: list[str], library_id: int) -> list[str]:
    if not terms:
        return []
    channel = (ContentNode.type == "table") if is_table else (ContentNode.type != "table")
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


async def _channel(is_table: bool, vectors: list[list[float]], terms: list[str], library_id: int) -> list[str]:
    # every dense (one per query variant) and the sparse search run concurrently, each on its own session, bounded by
    # the shared search semaphore — one question fans out into a single concurrent batch instead of serial round-trips
    searches = [_dense(is_table, vector, library_id) for vector in vectors]
    searches.append(_sparse(is_table, terms, library_id))
    return _rrf(await asyncio.gather(*searches))


async def _pending_nodes(library_id: int) -> list[_PendingNode]:
    # content-bearing nodes in this library with no embeddings row yet → a resumed run just re-selects the un-embedded
    # ones (anti-join, replacing the old embedding-IS-NULL-on-content trick now that embeddings live in their own table)
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(ContentNode.content_id, ContentNode.search_text, ContentNode.type, ContentNode.token_count)
            .join(Document, ContentNode.document_id == Document.id)
            .outerjoin(Embedding, Embedding.content_id == ContentNode.content_id)
            .where(
                Document.library_id == library_id,
                ContentNode.search_text.isnot(None),
                Embedding.content_id.is_(None),
            )
        )
    return [_PendingNode(row.content_id, row.search_text or "", row.type, row.token_count or 0) for row in rows]


def _upsert_chunks(records: list[dict[str, object]], size: int) -> Iterator[list[dict[str, object]]]:
    for start in range(0, len(records), size):
        yield records[start : start + size]


def _take_group(nodes: list[_PendingNode], start: int, budget: int) -> tuple[int, int]:
    total = nodes[start].token_len
    longest = nodes[start].token_len
    end = start + 1
    while end < len(nodes):
        tokens = nodes[end].token_len
        if total + tokens > budget:
            break
        total += tokens
        longest = max(longest, tokens)
        end += 1
    return end, longest


async def _embed_group(library_id: int, batch: list[_PendingNode], longest: int, label: str) -> int:
    total = sum(node.token_len for node in batch)  # real tokens packed vs the padded ceiling (rows x longest)
    logger.info("embed_group %s rows=%d longest=%d total=%d", label, len(batch), longest, total)
    vectors = await _embed([node.search_text for node in batch], longest)  # GPU work outside any open transaction
    records: list[dict[str, object]] = [
        {"content_id": node.content_id, "library_id": library_id, "type": node.node_type, "embedding": vector}
        for node, vector in zip(batch, vectors, strict=True)
    ]
    write_t = time.time()
    async with get_sessionmaker()() as session, session.begin():
        for chunk in _upsert_chunks(records, UPSERT_CHUNK):
            ins = pg_insert(Embedding).values(chunk)
            await session.execute(
                ins.on_conflict_do_update(index_elements=["content_id"], set_={"embedding": ins.excluded.embedding})
            )
    logger.info("embed_group %s write %.2fs", label, time.time() - write_t)
    return len(batch)


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
    # sort by length so every group is HOMOGENEOUS, which is what makes the memory estimate exact rather than merely
    # safe. sentence-transformers sorts each call internally and pads every mini-batch to ITS OWN max, so in a group
    # spanning 62..8192 tokens the group's `longest` is not the width any mini-batch actually runs at: we price them all
    # at the ceiling and are still wrong, over-reserving for the short ones and under-reserving for the worst one
    # (measured act 6695MiB against a 5548MiB budget — survived only on the 0.7 headroom). when every node in a group is
    # the same length, `longest` IS each mini-batch's padded width, the estimate becomes arithmetic, and no padding is
    # wasted. embed order is irrelevant — rows are upserted by content_id
    pending.sort(key=lambda node: node.token_len)
    total = len(pending)
    logger.info("embed library=%d nodes=%d", library_id, total)
    embedded = 0
    started = time.time()
    index = 0
    group_no = 0
    while index < total:
        end, longest = _take_group(pending, index, token_budget())
        group_no += 1
        embedded += await _embed_group(library_id, pending[index:end], longest, f"lib{library_id}.g{group_no}")
        index = end
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
    vectors = await _embed(queries, _max_tokens(queries))
    terms = _terms(queries)
    text, tables = await asyncio.gather(
        _channel(is_table=False, vectors=vectors, terms=terms, library_id=library_id),
        _channel(is_table=True, vectors=vectors, terms=terms, library_id=library_id),
    )
    return Retrieval(text=text[:CANDIDATES], tables=tables[:CANDIDATES])


async def load_passages(content_ids: list[str]) -> list[Passage]:
    if not content_ids:
        return []
    async with get_sessionmaker()() as session:
        rows = list(
            await session.execute(
                select(
                    ContentNode.content_id,
                    ContentNode.document_id,
                    ContentNode.parent_id,
                    ContentNode.page_no,
                    ContentNode.search_text,
                    Document.filename,
                )
                .join(Document, ContentNode.document_id == Document.id)
                .where(ContentNode.content_id.in_(content_ids))
            )
        )
    lookup = {row.content_id: row for row in rows}
    passages: list[Passage] = []
    for content_id in content_ids:
        row = lookup.get(content_id)
        if row is not None and row.search_text:
            page = f" p{row.page_no}" if row.page_no else ""
            passages.append(
                Passage(content_id, f"[{row.filename}{page}] {row.search_text}", row.document_id, row.parent_id)
            )
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
                    (table.anchors or {}).get("header_rows", []),
                )
            )
    return out
