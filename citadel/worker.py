import asyncio
import logging
import traceback
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from citadel.services import ingestion
from config import configure_logging, get_settings

logger = logging.getLogger(__name__)

BLOCK_MS = 5000

_tasks: set[asyncio.Task[None]] = set()


def _tb(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


async def _settle(stream: str, msg_id: str) -> None:
    redis = ingestion.get_redis()
    await redis.xack(stream, ingestion.GROUP, msg_id)
    await redis.xdel(stream, msg_id)  # acked entries (and page blobs) never accumulate


def _done(task: asyncio.Task[None]) -> None:
    _tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("worker task crashed\n%s", _tb(task.exception()))


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)  # strong ref so the in-flight task isn't GC'd
    task.add_done_callback(_done)


async def _retry_or_fail(
    stream: str, msg_id: str, raw: dict[bytes, bytes], giveup: Callable[[], Awaitable[None]]
) -> None:
    # the handler RAISED (worker is alive, it knows it failed): requeue with a bumped attempt, or give up
    redis = ingestion.get_redis()
    attempt = int(raw.get(b"attempt", b"0")) + 1
    if attempt > ingestion.MAX_ATTEMPTS:
        await giveup()
    else:
        await redis.xadd(stream, {**raw, b"attempt": str(attempt).encode()})
    await _settle(stream, msg_id)


async def _recover(stream: str, consumer: str, spawn: Callable[[str, dict[bytes, bytes]], None]) -> None:
    # startup only: reclaim whatever a previous (crashed) run left pending and reprocess it (idempotent)
    redis = ingestion.get_redis()
    cursor = b"0-0"
    while True:
        cursor, claimed, _ = await redis.xautoclaim(
            stream, ingestion.GROUP, consumer, min_idle_time=0, start_id=cursor, count=64
        )
        for msg_id, raw in claimed:
            spawn(msg_id.decode(), raw)
        if cursor == b"0-0":
            return


# ---- per-job work: run the handler as a sub-task, settle on success, requeue/giveup on failure ----------
async def _normalize_job(profiles: asyncio.Queue[str], msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = ingestion.STREAM_INGEST
    profile = await profiles.get()  # blocks until a libreoffice profile frees up → bounds concurrency
    try:
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        work = asyncio.create_task(ingestion.handle_normalize(fields, profile))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("normalize failed file=%s\n%s", fields.get("filename", "?"), _tb(work.exception()))
        await _retry_or_fail(
            stream, msg_id, raw, lambda: ingestion.fail_document(fields.get("doc_id", ""), "normalize")
        )
    finally:
        profiles.put_nowait(profile)


async def _paginate_job(sem: asyncio.Semaphore, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = ingestion.STREAM_NORMALIZED
    async with sem:
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        work = asyncio.create_task(ingestion.handle_paginate(fields))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("paginate failed file=%s\n%s", fields.get("filename", "?"), _tb(work.exception()))
        await _retry_or_fail(stream, msg_id, raw, lambda: ingestion.fail_document(fields.get("doc_id", ""), "paginate"))


async def _ocr_job(sem: asyncio.Semaphore, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = ingestion.STREAM_PAGES
    async with sem:
        fields = {k.decode(): v.decode() for k, v in raw.items() if k != b"image"}
        work = asyncio.create_task(ingestion.handle_ocr(fields, raw.get(b"image", b"")))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("ocr failed page=%s\n%s", fields.get("page_idx", "?"), _tb(work.exception()))
        await _retry_or_fail(
            stream, msg_id, raw, lambda: ingestion.fail_page(fields["doc_id"], int(fields["page_idx"]))
        )


async def _merge_job(sem: asyncio.Semaphore, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = ingestion.STREAM_MERGE
    async with sem:
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        work = asyncio.create_task(ingestion.handle_merge(fields))
        await asyncio.wait({work})
        if work.exception() is not None:
            logger.error("merge failed doc=%s\n%s", fields.get("doc_id", "?"), _tb(work.exception()))
            await _retry_or_fail(
                stream, msg_id, raw, lambda: ingestion.fail_document(fields.get("doc_id", ""), "merge")
            )
            return
        await _settle(stream, msg_id)
        await ingestion.cleanup(fields["doc_id"])  # only after the merge message is acked → safe to delete blocks


# ---- normalize: read `ingest`, convert, write `normalized`. one dedicated libreoffice profile per worker
async def normalize() -> None:
    stream = ingestion.STREAM_INGEST
    redis = ingestion.get_redis()
    await ingestion.ensure_group(stream)
    consumer = uuid.uuid4().hex
    limit = get_settings().normalize_concurrency
    profiles = ingestion.make_profile_pool(limit)
    logger.info("consuming %s concurrency=%d", stream, limit)
    await _recover(stream, consumer, lambda mid, raw: _spawn(_normalize_job(profiles, mid, raw)))
    while True:
        fresh = await redis.xreadgroup(ingestion.GROUP, consumer, {stream: ">"}, count=limit, block=BLOCK_MS)
        for msg_id, raw in fresh[0][1] if fresh else []:
            _spawn(_normalize_job(profiles, msg_id.decode(), raw))


# ---- paginate: read `normalized`, render pages, write `pages`. dedicated pdfium per process-pool worker
async def paginate() -> None:
    stream = ingestion.STREAM_NORMALIZED
    redis = ingestion.get_redis()
    await ingestion.ensure_group(stream)
    consumer = uuid.uuid4().hex
    limit = get_settings().paginate_concurrency
    sem = asyncio.Semaphore(limit)
    logger.info("consuming %s concurrency=%d", stream, limit)
    await _recover(stream, consumer, lambda mid, raw: _spawn(_paginate_job(sem, mid, raw)))
    while True:
        fresh = await redis.xreadgroup(ingestion.GROUP, consumer, {stream: ">"}, count=limit, block=BLOCK_MS)
        for msg_id, raw in fresh[0][1] if fresh else []:
            _spawn(_paginate_job(sem, msg_id.decode(), raw))


# ---- ocr: read `pages`, extract via vLLM, write blocks + `merge`. one global concurrency bound ----------
async def ocr() -> None:
    stream = ingestion.STREAM_PAGES
    redis = ingestion.get_redis()
    await ingestion.ensure_group(stream)
    consumer = uuid.uuid4().hex
    limit = get_settings().ocr_concurrency
    sem = asyncio.Semaphore(limit)
    logger.info("consuming %s concurrency=%d", stream, limit)
    await _recover(stream, consumer, lambda mid, raw: _spawn(_ocr_job(sem, mid, raw)))
    while True:
        fresh = await redis.xreadgroup(ingestion.GROUP, consumer, {stream: ">"}, count=limit, block=BLOCK_MS)
        for msg_id, raw in fresh[0][1] if fresh else []:
            _spawn(_ocr_job(sem, msg_id.decode(), raw))


# ---- merge: read `merge`, assemble result.json, clean up. light concurrency ----------------------------
async def merge() -> None:
    stream = ingestion.STREAM_MERGE
    redis = ingestion.get_redis()
    await ingestion.ensure_group(stream)
    consumer = uuid.uuid4().hex
    limit = get_settings().merge_concurrency
    sem = asyncio.Semaphore(limit)
    logger.info("consuming %s concurrency=%d", stream, limit)
    await _recover(stream, consumer, lambda mid, raw: _spawn(_merge_job(sem, mid, raw)))
    while True:
        fresh = await redis.xreadgroup(ingestion.GROUP, consumer, {stream: ">"}, count=limit, block=BLOCK_MS)
        for msg_id, raw in fresh[0][1] if fresh else []:
            _spawn(_merge_job(sem, msg_id.decode(), raw))


async def _main() -> None:
    await asyncio.gather(normalize(), paginate(), ocr(), merge())


def main() -> None:
    configure_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
