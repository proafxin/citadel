import asyncio
import logging
import os
import traceback
import uuid
from collections.abc import Coroutine
from typing import Any

from citadel.services import ingestion
from config import configure_logging, get_settings

logger = logging.getLogger(__name__)

BLOCK_MS = 5000
IDLE_MS = 60000
CPU = os.cpu_count() or 4

_tasks: set[asyncio.Task[None]] = set()


async def _settle(stream: str, msg_id: str) -> None:
    redis = ingestion.get_redis()
    await redis.xack(stream, ingestion.GROUP, msg_id)
    await redis.hdel(f"attempts:{stream}", msg_id)
    await redis.xdel(stream, msg_id)  # acked entries (and page blobs) never accumulate


def _done(task: asyncio.Task[None]) -> None:
    # a job that raised is left unacked → reclaimed by xautoclaim and retried; just drop the ref + log
    _tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("job crashed\n%s", "".join(traceback.format_exception(task.exception())))


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)  # strong ref so the in-flight task isn't GC'd
    task.add_done_callback(_done)


# ---- per-job work: bounded by the stage semaphore, settle on success, stay unacked on failure ----------
async def _normalize_job(sem: asyncio.Semaphore, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = ingestion.STREAM_INGEST
    async with sem:
        redis = ingestion.get_redis()
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        if await redis.hincrby(f"attempts:{stream}", msg_id, 1) > ingestion.MAX_ATTEMPTS:
            await ingestion.fail_document(fields.get("doc_id", ""), "normalize")
            await _settle(stream, msg_id)
            return
        await ingestion.handle_normalize(fields)
        await _settle(stream, msg_id)


async def _paginate_job(sem: asyncio.Semaphore, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = ingestion.STREAM_NORMALIZED
    async with sem:
        redis = ingestion.get_redis()
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        if await redis.hincrby(f"attempts:{stream}", msg_id, 1) > ingestion.MAX_ATTEMPTS:
            await ingestion.fail_document(fields.get("doc_id", ""), "paginate")
            await _settle(stream, msg_id)
            return
        await ingestion.handle_paginate(fields)
        await _settle(stream, msg_id)


async def _ocr_job(sem: asyncio.Semaphore, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = ingestion.STREAM_PAGES
    async with sem:
        redis = ingestion.get_redis()
        fields = {k.decode(): v.decode() for k, v in raw.items() if k != b"image"}
        if await redis.hincrby(f"attempts:{stream}", msg_id, 1) > ingestion.MAX_ATTEMPTS:
            await ingestion.fail_page(fields["doc_id"], int(fields["page_idx"]))
            await _settle(stream, msg_id)
            return
        await ingestion.handle_ocr(fields, raw.get(b"image", b""))
        await _settle(stream, msg_id)


async def _merge_job(sem: asyncio.Semaphore, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = ingestion.STREAM_MERGE
    async with sem:
        redis = ingestion.get_redis()
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        if await redis.hincrby(f"attempts:{stream}", msg_id, 1) > ingestion.MAX_ATTEMPTS:
            await ingestion.fail_document(fields.get("doc_id", ""), "merge")
            await _settle(stream, msg_id)
            return
        await ingestion.handle_merge(fields)
        await _settle(stream, msg_id)


# ---- normalize: read `ingest`, convert, write `normalized`. non-blocking, bounded concurrency ----------
async def normalize() -> None:
    stream = ingestion.STREAM_INGEST
    redis = ingestion.get_redis()
    await ingestion.ensure_group(stream)
    consumer = uuid.uuid4().hex
    sem = asyncio.Semaphore(CPU)
    logger.info("consuming %s concurrency=%d", stream, CPU)
    while True:
        _cursor, stale, _deleted = await redis.xautoclaim(
            stream, ingestion.GROUP, consumer, min_idle_time=IDLE_MS, count=CPU
        )
        fresh = await redis.xreadgroup(ingestion.GROUP, consumer, {stream: ">"}, count=CPU, block=BLOCK_MS)
        for msg_id, raw in stale + (fresh[0][1] if fresh else []):
            _spawn(_normalize_job(sem, msg_id.decode(), raw))


# ---- paginate: read `normalized`, render pages, write `pages`. non-blocking, bounded concurrency -------
async def paginate() -> None:
    stream = ingestion.STREAM_NORMALIZED
    redis = ingestion.get_redis()
    await ingestion.ensure_group(stream)
    consumer = uuid.uuid4().hex
    sem = asyncio.Semaphore(CPU)
    logger.info("consuming %s concurrency=%d", stream, CPU)
    while True:
        _cursor, stale, _deleted = await redis.xautoclaim(
            stream, ingestion.GROUP, consumer, min_idle_time=IDLE_MS, count=CPU
        )
        fresh = await redis.xreadgroup(ingestion.GROUP, consumer, {stream: ">"}, count=CPU, block=BLOCK_MS)
        for msg_id, raw in stale + (fresh[0][1] if fresh else []):
            _spawn(_paginate_job(sem, msg_id.decode(), raw))


# ---- ocr: read `pages`, extract via vLLM, write blocks + `merge`. globally bounded concurrency ----------
async def ocr() -> None:
    stream = ingestion.STREAM_PAGES
    redis = ingestion.get_redis()
    await ingestion.ensure_group(stream)
    consumer = uuid.uuid4().hex
    limit = get_settings().ocr_concurrency
    sem = asyncio.Semaphore(limit)
    logger.info("consuming %s concurrency=%d", stream, limit)
    while True:
        _cursor, stale, _deleted = await redis.xautoclaim(
            stream, ingestion.GROUP, consumer, min_idle_time=IDLE_MS, count=limit
        )
        fresh = await redis.xreadgroup(ingestion.GROUP, consumer, {stream: ">"}, count=limit, block=BLOCK_MS)
        for msg_id, raw in stale + (fresh[0][1] if fresh else []):
            _spawn(_ocr_job(sem, msg_id.decode(), raw))


# ---- merge: read `merge`, assemble result.json, clean up. non-blocking, bounded concurrency ------------
async def merge() -> None:
    stream = ingestion.STREAM_MERGE
    redis = ingestion.get_redis()
    await ingestion.ensure_group(stream)
    consumer = uuid.uuid4().hex
    sem = asyncio.Semaphore(CPU)
    logger.info("consuming %s concurrency=%d", stream, CPU)
    while True:
        _cursor, stale, _deleted = await redis.xautoclaim(
            stream, ingestion.GROUP, consumer, min_idle_time=IDLE_MS, count=CPU
        )
        fresh = await redis.xreadgroup(ingestion.GROUP, consumer, {stream: ">"}, count=CPU, block=BLOCK_MS)
        for msg_id, raw in stale + (fresh[0][1] if fresh else []):
            _spawn(_merge_job(sem, msg_id.decode(), raw))


async def _main() -> None:
    await asyncio.gather(normalize(), paginate(), ocr(), merge())


def main() -> None:
    configure_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
