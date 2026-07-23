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
from citadel.models.batch import ContentBatch
from citadel.models.content import ContentNode
from citadel.models.document import Document
from citadel.models.embedding import Embedding
from citadel.models.library import Library
from citadel.models.status import DocumentStatus
from citadel.models.table import Table
from citadel.services.batching import render_block
from config import get_embedder

logger = logging.getLogger(__name__)

EMBED_TTL = 86_400

RRF_K = 60
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
    content_id: int
    table_id: int
    filename: str
    n_rows: int
    columns: list[dict]
    metadata: dict
    sample_rows: list[list]
    header_rows: list[int]  # row_idx values in table_rows that are header, not data — the query projection skips them


@dataclass
class _PendingNode:
    content_id: int
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


def _rrf(rankings: list[list[int]]) -> list[int]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, content_id in enumerate(ranking, start=1):
            scores[content_id] = scores.get(content_id, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores, key=lambda content_id: scores[content_id], reverse=True)


async def _pending_nodes(library_id: int) -> list[_PendingNode]:
    # content-bearing nodes in this library with no embeddings row yet → a resumed run just re-selects the un-embedded
    # ones (anti-join, replacing the old embedding-IS-NULL-on-content trick now that embeddings live in their own table)
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(ContentNode.id, ContentNode.search_text, ContentNode.type, ContentNode.token_count)
            .join(Document, ContentNode.document_id == Document.id)
            .outerjoin(Embedding, Embedding.content_id == ContentNode.id)
            .where(
                Document.library_id == library_id,
                ContentNode.search_text.isnot(None),
                Embedding.content_id.is_(None),
            )
        )
    return [_PendingNode(row.id, row.search_text or "", row.type, row.token_count or 0) for row in rows]


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


async def _encode_group(
    library_id: int, batch: list[_PendingNode], longest: int, label: str
) -> list[dict[str, object]]:
    total = sum(node.token_len for node in batch)  # real tokens packed vs the padded ceiling (rows x longest)
    logger.info("embed_group %s rows=%d longest=%d total=%d", label, len(batch), longest, total)
    vectors = await _embed([node.search_text for node in batch], longest)  # GPU work outside any open transaction
    return [
        {"content_id": node.content_id, "library_id": library_id, "type": node.node_type, "embedding": vector}
        for node, vector in zip(batch, vectors, strict=True)
    ]


async def _write_group(records: list[dict[str, object]], label: str) -> None:
    # runs CONCURRENTLY with the next group's encode. the two contend for nothing — this is postgres I/O, that is the
    # GPU — and the write is half of finalize, so leaving it on the critical path idles the card for its whole
    # duration. deferring the writes to the end instead would save nothing: the cost is ~1ms PER ROW of hnsw graph
    # maintenance, flat across group sizes, so it is paid whenever it happens. only overlap removes it from the clock
    write_t = time.time()
    async with get_sessionmaker()() as session, session.begin():
        for chunk in _upsert_chunks(records, UPSERT_CHUNK):
            ins = pg_insert(Embedding).values(chunk)
            await session.execute(
                ins.on_conflict_do_update(index_elements=["content_id"], set_={"embedding": ins.excluded.embedding})
            )
    logger.info("embed_group %s write %.2fs", label, time.time() - write_t)


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
    writing: asyncio.Task[None] | None = None
    while index < total:
        end, longest = _take_group(pending, index, token_budget())
        group_no += 1
        label = f"lib{library_id}.g{group_no}"
        records = await _encode_group(library_id, pending[index:end], longest, label)
        if writing is not None:
            await writing  # one open transaction at a time, and a failed write raises here rather than being lost
        writing = asyncio.create_task(_write_group(records, label))
        embedded += end - index
        index = end
        elapsed = time.time() - started
        rate = embedded / elapsed if elapsed > 0 else 0.0
        await redis.hset(f"embed:{library_id}", mapping={"done": embedded, "total": total})
        logger.info("embed library=%d %d/%d nodes %.1fs %.0f nodes/s", library_id, embedded, total, elapsed, rate)
    if writing is not None:
        await writing  # the last group's write must land before any document is marked embedded
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


def _table_cand(table: Table, filename: str) -> TableCand:
    return TableCand(
        table.content_id,
        table.id,
        filename,
        table.n_rows,
        table.columns,
        table.table_metadata,
        table.sample_rows,
        (table.anchors or {}).get("header_rows", []),
    )


async def load_all_tables(library_id: int) -> list[TableCand]:
    # the table channel sees every table in the library — schema + samples + row count — and writes the queries. no
    # retrieval prefilter: relevance is decided constructively by which tables the queries reference
    async with get_sessionmaker()() as session:
        rows = list(
            await session.execute(
                select(Table, Document.filename)
                .join(Document, Table.document_id == Document.id)
                .where(Document.library_id == library_id)
                .order_by(Table.id)
            )
        )
    return [_table_cand(table, filename) for table, filename in rows]


async def _scoped_dense(vector: list[float], content_ids: list[int]) -> list[int]:
    stmt = (
        select(Embedding.content_id)
        .where(Embedding.content_id.in_(content_ids), Embedding.type != "table")
        .order_by(Embedding.embedding.cosine_distance(vector))
    )
    async with _search_semaphore(), get_sessionmaker()() as session:
        return list(await session.scalars(stmt))


async def _scoped_sparse(terms: list[str], content_ids: list[int]) -> list[int]:
    if not terms:
        return []
    conditions = [ContentNode.search_text.ilike(_like(term), escape="\\") for term in terms]
    hits = functools.reduce(operator.add, (case((cond, 1), else_=0) for cond in conditions))
    stmt = (
        select(ContentNode.id)
        .where(ContentNode.id.in_(content_ids), ContentNode.search_text.isnot(None), or_(*conditions))
        .order_by(hits.desc())
    )
    async with _search_semaphore(), get_sessionmaker()() as session:
        return list(await session.scalars(stmt))


async def rank_blocks(vectors: list[list[float]], terms: list[str], content_ids: list[int]) -> list[int]:
    # ONE pooled net over every block of every selected batch, so cross-batch relevance is on a single scale. dense and
    # sparse fused by RRF; this is the corpus-wide net of before, now scoped to the batches the summaries selected
    if not content_ids:
        return []
    searches = [_scoped_dense(vector, content_ids) for vector in vectors]
    searches.append(_scoped_sparse(terms, content_ids))
    return _rrf(await asyncio.gather(*searches))


async def rank_scoped(question: str, content_ids: list[int]) -> list[int]:
    if not content_ids:
        return []
    vectors = await embed_query([question])
    return await rank_blocks(vectors, _terms([question]), content_ids)


async def scope_block_ids(ranges: list[tuple[int, int, int]]) -> list[int]:
    # content ids of the blocks under the selected batches, addressed by (document_id, block_ordinal range). table
    # nodes are INCLUDED: a tabular document's content is its tables, rendered as schema + samples, and it must
    # contribute to the answer exactly as a text document's prose does — the same content the batch already counted
    if not ranges:
        return []
    clauses = [
        (ContentNode.document_id == doc_id)
        & (ContentNode.block_ordinal >= start)
        & (ContentNode.block_ordinal <= end)
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
        body = render_block(node, tables.get(node.id))  # prose renders its raw; a table renders schema + samples
        if body.strip():
            page = f" p{node.page_no}" if node.page_no else ""
            out[node.id] = BlockText(node.id, node.document_id, node.block_ordinal or 0, f"[{filename}{page}] {body}")
    return out


async def embed_query(queries: list[str]) -> list[list[float]]:
    return await _embed(queries, _max_tokens(queries))


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


async def load_library_batches(library_id: int) -> list[BatchRef]:
    # every batch summary in the library, in a stable order (document, then batch). this is the text channel's whole
    # input — the corpus in its compressed form
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
