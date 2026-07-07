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

from citadel.db import get_sessionmaker
from citadel.models.content import ContentNode
from citadel.models.document import Document
from citadel.models.embedding import Embedding
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

EMBED_VRAM_HEADROOM = 0.7  # fraction of currently-free VRAM one encode may use; margin for fit error + spikes
MODEL_MAX_TOKENS = 8192  # BGE-M3 context ceiling — the encoder truncates here, so it caps effective seq length
BOOTSTRAP_A = 40_000.0  # safe-high per-token seed used only while the startup sweep is still climbing to long lengths
CALIB_TOKENS = (256, 512, 1024, 2048, 4096, 8192)  # startup sweep points (short→long so early fits size the next batch)
CALIB_BATCH_MAX = 32  # cap the calibration batch so the sweep stays quick
CALIB_HEADROOM = 0.5  # extra-conservative VRAM fraction for the calibration batches themselves
UPSERT_COLS = 4  # content_id, library_id, type, embedding per embeddings row
UPSERT_CHUNK = 32767 // UPSERT_COLS  # asyncpg caps bind params per statement at 32767 → chunk the multi-row upsert

_EMBED_LOCK = asyncio.Lock()
# per-item activation ≈ a·L + b·L² (L = seq tokens): fitted online by OLS from measured encodes so batch sizing is
# VRAM-accurate INCLUDING the superlinear (attention/workspace) term. sums are touched only by the encode thread; the
# published model tuple is one atomic dict key the sizing loop reads (encodes are serialized, so no real contention).
_calib_sums: dict[str, float] = {"s2": 0.0, "s3": 0.0, "s4": 0.0, "t1": 0.0, "t2": 0.0, "n": 0.0}
_calib_model: dict[str, tuple[float, float]] = {"ab": (BOOTSTRAP_A, 0.0)}


def _max_tokens(texts: list[str]) -> int:
    # true token length via the model's own tokenizer; only the longest-by-chars candidates hold the longest-by-tokens
    tokenizer = get_embedder().tokenizer
    candidates = sorted(texts, key=len, reverse=True)[:16]
    encoded = tokenizer(candidates, add_special_tokens=True)["input_ids"]
    return min(MODEL_MAX_TOKENS, max((len(ids) for ids in encoded), default=1))


def _update_calib(activation: int, batch: int, longest: int) -> None:
    per_item = activation / batch
    length = float(longest)
    _calib_sums["s2"] += length**2
    _calib_sums["s3"] += length**3
    _calib_sums["s4"] += length**4
    _calib_sums["t1"] += length * per_item
    _calib_sums["t2"] += length**2 * per_item
    _calib_sums["n"] += 1
    det = _calib_sums["s2"] * _calib_sums["s4"] - _calib_sums["s3"] ** 2
    if _calib_sums["n"] >= 2 and det > 0:  # solve the 2x2 OLS normal equations for a, b; clamp unphysical negatives
        a = (_calib_sums["t1"] * _calib_sums["s4"] - _calib_sums["t2"] * _calib_sums["s3"]) / det
        b = (_calib_sums["s2"] * _calib_sums["t2"] - _calib_sums["s3"] * _calib_sums["t1"]) / det
        _calib_model["ab"] = (max(a, 0.0), max(b, 0.0))


def _per_item(longest: int) -> float:
    a, b = _calib_model["ab"]
    return a * longest + b * longest * longest


def calibrate_embedder() -> None:
    # startup VRAM sweep: fit per_item(L)=a·L+b·L² by encoding synthetic batches at known sequence lengths, so batch
    # sizing is accurate before any real embedding. activation is content-independent (driven by batch × seq), so
    # synthetic text is representative. short→long: each measured point refines the model that sizes the next batch.
    tokenizer = get_embedder().tokenizer
    for target in CALIB_TOKENS:
        text = "data " * target
        longest = min(MODEL_MAX_TOKENS, len(tokenizer(text, add_special_tokens=True)["input_ids"]))
        free, _ = torch.cuda.mem_get_info()
        batch = max(1, min(CALIB_BATCH_MAX, int(free * CALIB_HEADROOM / _per_item(longest))))
        torch.cuda.reset_peak_memory_stats()
        baseline = torch.cuda.memory_allocated()
        get_embedder().encode([text] * batch, batch_size=batch, normalize_embeddings=True, show_progress_bar=False)
        activation = torch.cuda.max_memory_allocated() - baseline
        if activation > 0:
            _update_calib(activation, batch, longest)
        a, b = _calib_model["ab"]
        logger.info("calibrate longest=%d batch=%d act=%dMiB a=%.0f b=%.4f", longest, batch, activation >> 20, a, b)


def embed_texts(texts: list[str], longest: int) -> list[list[float]]:
    # the group is pre-sized to fit one pass at the VRAM budget, so encode it whole (batch_size = len). the peak is
    # logged (not fitted — the model is fixed by the startup sweep) so a misprediction is visible against free.
    if not texts:
        return []
    torch.cuda.reset_peak_memory_stats()
    baseline = torch.cuda.memory_allocated()
    vectors = get_embedder().encode(texts, batch_size=len(texts), normalize_embeddings=True, show_progress_bar=False)
    activation = torch.cuda.max_memory_allocated() - baseline
    free, _ = torch.cuda.mem_get_info()
    logger.info(
        "embed_encode rows=%d longest=%d act=%dMiB free=%dMiB", len(texts), longest, activation >> 20, free >> 20
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


@dataclass
class Retrieval:
    text: list[str]
    tables: list[str]


@dataclass
class Passage:
    content_id: str
    text: str


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


def _take_group(nodes: list[_PendingNode], start: int, budget: float) -> tuple[int, int]:
    # iterate in original order; add a node while the estimated VRAM of the batch still fits the budget, else flush.
    # a batch pads to its longest sequence, so estimated VRAM = count × per_item(longest). always takes ≥1 node.
    longest = nodes[start].token_len
    end = start + 1
    while end < len(nodes):
        candidate_longest = max(longest, nodes[end].token_len)
        if (end - start + 1) * _per_item(candidate_longest) > budget:
            break
        longest = candidate_longest
        end += 1
    return end, longest


async def _embed_group(library_id: int, batch: list[_PendingNode], longest: int, label: str) -> int:
    total = sum(node.token_len for node in batch)  # real tokens packed vs the padded ceiling (rows × longest)
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
    total = len(pending)
    logger.info("embed library=%d nodes=%d", library_id, total)
    embedded = 0
    started = time.time()
    index = 0
    group_no = 0
    while index < total:
        free, _ = torch.cuda.mem_get_info()
        end, longest = _take_group(pending, index, EMBED_VRAM_HEADROOM * free)
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
                select(ContentNode.content_id, Document.filename, ContentNode.page_no, ContentNode.search_text)
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
            passages.append(Passage(content_id, f"[{row.filename}{page}] {row.search_text}"))
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
