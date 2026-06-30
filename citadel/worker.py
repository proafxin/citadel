import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from citadel.services.ingestion import (
    GROUP,
    MAX_ATTEMPTS,
    STREAM_GAPFILL,
    STREAM_INGEST,
    STREAM_MERGE,
    STREAM_NORMALIZED,
    STREAM_PAGES,
    cleanup,
    emit_vlm_only,
    ensure_group,
    fail_document,
    fail_page,
    get_mineru_client,
    get_redis,
    handle_gapfill,
    handle_merge,
    handle_normalize,
    handle_ocr,
    handle_paginate,
    make_profile_pool,
)
from config import configure_logging, get_settings

logger = logging.getLogger(__name__)

BLOCK_MS = 5000

_tasks: set[asyncio.Task[None]] = set()


def _tb(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


async def _settle(stream: str, msg_id: str) -> None:
    redis = get_redis()
    await redis.xack(stream, GROUP, msg_id)
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
    redis = get_redis()
    attempt = int(raw.get(b"attempt", b"0")) + 1
    if attempt > MAX_ATTEMPTS:
        await giveup()
    else:
        await redis.xadd(stream, {**raw, b"attempt": str(attempt).encode()})
    await _settle(stream, msg_id)


async def _recover(stream: str, consumer: str, spawn: Callable[[str, dict[bytes, bytes]], None]) -> None:
    # startup only: reclaim whatever a previous (crashed) run left pending and reprocess it (idempotent)
    redis = get_redis()
    cursor = b"0-0"
    while True:
        cursor, claimed, _ = await redis.xautoclaim(stream, GROUP, consumer, min_idle_time=0, start_id=cursor, count=64)
        for msg_id, raw in claimed:
            spawn(msg_id.decode(), raw)
        if cursor == b"0-0":
            return


# ---- per-job work: run the handler as a sub-task, settle on success, requeue/giveup on failure ----------
async def _normalize_job(profiles: asyncio.Queue[str], msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_INGEST
    profile = await profiles.get()  # blocks until a libreoffice profile frees up → bounds concurrency
    try:
        fields = {k.decode(): v.decode() for k, v in raw.items() if k != b"data"}
        work = asyncio.create_task(handle_normalize(fields, profile, raw.get(b"data", b"")))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("normalize failed file=%s\n%s", fields.get("filename", "?"), _tb(work.exception()))
        await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "normalize"))
    finally:
        profiles.put_nowait(profile)


async def _paginate_job(sem: asyncio.Semaphore, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_NORMALIZED
    async with sem:
        fields = {k.decode(): v.decode() for k, v in raw.items() if k != b"data"}
        work = asyncio.create_task(handle_paginate(fields, raw.get(b"data", b"")))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("paginate failed file=%s\n%s", fields.get("filename", "?"), _tb(work.exception()))
        await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "paginate"))


async def _ocr_job(sem: asyncio.Semaphore, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_PAGES
    async with sem:
        fields = {k.decode(): v.decode() for k, v in raw.items() if k != b"image"}
        work = asyncio.create_task(handle_ocr(fields, raw.get(b"image", b"")))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("ocr failed page=%s\n%s", fields.get("page_idx", "?"), _tb(work.exception()))
        await _retry_or_fail(stream, msg_id, raw, lambda: fail_page(fields["doc_id"], int(fields["page_idx"])))


async def _gapfill_job(sem: asyncio.Semaphore, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_GAPFILL
    async with sem:
        fields = {k.decode(): v.decode() for k, v in raw.items() if k != b"image"}
        work = asyncio.create_task(handle_gapfill(fields, raw.get(b"image", b"")))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("gapfill failed page=%s\n%s", fields.get("page_idx", "?"), _tb(work.exception()))
        # retries exhausted → finalize with the VLM blocks alone so a RapidOCR error never loses the page
        await _retry_or_fail(stream, msg_id, raw, lambda: emit_vlm_only(fields))


async def _merge_job(sem: asyncio.Semaphore, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_MERGE
    async with sem:
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        work = asyncio.create_task(handle_merge(fields))
        await asyncio.wait({work})
        if work.exception() is not None:
            logger.error("merge failed doc=%s\n%s", fields.get("doc_id", "?"), _tb(work.exception()))
            await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "merge"))
            return
        await _settle(stream, msg_id)
        await cleanup(fields["doc_id"])  # only after the merge message is acked → safe to delete blocks


# ---- normalize: read `ingest`, convert, write `normalized`. one dedicated libreoffice profile per worker
async def normalize() -> None:
    stream = STREAM_INGEST
    redis = get_redis()
    await ensure_group(stream)
    consumer = f"{stream}-{get_settings().worker_id}"
    limit = get_settings().normalize_concurrency
    profiles = make_profile_pool(limit)
    logger.info("consuming %s concurrency=%d", stream, limit)
    await _recover(stream, consumer, lambda mid, raw: _spawn(_normalize_job(profiles, mid, raw)))
    while True:
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: ">"}, count=limit, block=BLOCK_MS)
        for msg_id, raw in fresh[0][1] if fresh else []:
            _spawn(_normalize_job(profiles, msg_id.decode(), raw))


# ---- paginate: read `normalized`, render pages, write `pages`. dedicated pdfium per process-pool worker
async def paginate() -> None:
    stream = STREAM_NORMALIZED
    redis = get_redis()
    await ensure_group(stream)
    consumer = f"{stream}-{get_settings().worker_id}"
    limit = get_settings().paginate_concurrency
    sem = asyncio.Semaphore(limit)
    logger.info("consuming %s concurrency=%d", stream, limit)
    await _recover(stream, consumer, lambda mid, raw: _spawn(_paginate_job(sem, mid, raw)))
    while True:
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: ">"}, count=limit, block=BLOCK_MS)
        for msg_id, raw in fresh[0][1] if fresh else []:
            _spawn(_paginate_job(sem, msg_id.decode(), raw))


# ---- ocr: read `pages`, extract via vLLM, write blocks + `merge`. one global concurrency bound ----------
async def ocr() -> None:
    stream = STREAM_PAGES
    redis = get_redis()
    await ensure_group(stream)
    await asyncio.to_thread(get_mineru_client)  # build the http client once now, not lazily mid-OCR
    consumer = f"{stream}-{get_settings().worker_id}"
    limit = get_settings().ocr_concurrency
    sem = asyncio.Semaphore(limit)
    logger.info("consuming %s concurrency=%d", stream, limit)
    await _recover(stream, consumer, lambda mid, raw: _spawn(_ocr_job(sem, mid, raw)))
    while True:
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: ">"}, count=limit, block=BLOCK_MS)
        for msg_id, raw in fresh[0][1] if fresh else []:
            _spawn(_ocr_job(sem, msg_id.decode(), raw))


# ---- gapfill: read `gapfill`, RapidOCR the lines the VLM dropped, finalize the page. CPU-bound, bounded low ----
async def gapfill() -> None:
    stream = STREAM_GAPFILL
    redis = get_redis()
    await ensure_group(stream)
    consumer = f"{stream}-{get_settings().worker_id}"
    limit = get_settings().rapidocr_concurrency  # match the RapidOCR thread pool → ≤limit decoded page arrays resident
    sem = asyncio.Semaphore(limit)
    logger.info("consuming %s concurrency=%d", stream, limit)
    await _recover(stream, consumer, lambda mid, raw: _spawn(_gapfill_job(sem, mid, raw)))
    while True:
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: ">"}, count=limit, block=BLOCK_MS)
        for msg_id, raw in fresh[0][1] if fresh else []:
            _spawn(_gapfill_job(sem, msg_id.decode(), raw))


# ---- merge: read `merge`, assemble result.json, clean up. light concurrency ----------------------------
async def merge() -> None:
    stream = STREAM_MERGE
    redis = get_redis()
    await ensure_group(stream)
    consumer = f"{stream}-{get_settings().worker_id}"
    limit = get_settings().merge_concurrency
    sem = asyncio.Semaphore(limit)
    logger.info("consuming %s concurrency=%d", stream, limit)
    await _recover(stream, consumer, lambda mid, raw: _spawn(_merge_job(sem, mid, raw)))
    while True:
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: ">"}, count=limit, block=BLOCK_MS)
        for msg_id, raw in fresh[0][1] if fresh else []:
            _spawn(_merge_job(sem, msg_id.decode(), raw))


async def _main() -> None:
    await asyncio.gather(normalize(), paginate(), ocr(), gapfill(), merge())


def main() -> None:
    configure_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
