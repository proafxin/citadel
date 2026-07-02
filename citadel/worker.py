import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from citadel.llm import SLM_CONCURRENCY
from citadel.services.ingestion import (
    GROUP,
    MAX_ATTEMPTS,
    PAGINATE_CONCURRENCY,
    RAPIDOCR_CONCURRENCY,
    STREAM_GAPFILL,
    STREAM_INGEST,
    STREAM_MERGE,
    STREAM_NORMALIZED,
    STREAM_PAGES,
    STREAM_TABLES,
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
    handle_tabular,
    make_profile_pool,
)
from config import CPU_HALF, configure_logging, get_settings

logger = logging.getLogger(__name__)

NORMALIZE_CONCURRENCY = CPU_HALF  # one isolated libreoffice profile per worker (per-job soffice)
OCR_CONCURRENCY = 128  # pages in flight; sockets are capped by MINERU_MAX_CONNECTIONS, so keep this high to feed the server (born-digital makes few requests/page → needs many concurrent pages)
MERGE_CONCURRENCY = 4  # light assembly

BLOCK_MS = 5000
RECLAIM_BATCH = 64

_tasks: set[asyncio.Task[None]] = set()

Spawn = Callable[[str, dict[bytes, bytes]], None]


@dataclass
class _Capacity:
    # bounds in-flight messages, not just compute: the dispatch loop reads at most free() messages, so a backlog
    # never pulls more than `limit` claimed entries (and their image/file blobs) into worker RAM at once
    limit: int
    inflight: int = 0
    slot: asyncio.Event = field(default_factory=asyncio.Event)

    def free(self) -> int:
        return self.limit - self.inflight

    def take(self) -> None:
        self.inflight += 1

    def release(self) -> None:
        self.inflight -= 1
        self.slot.set()

    async def wait_free(self) -> None:
        self.slot.clear()
        if self.free() > 0:
            return
        await self.slot.wait()


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


async def _recover(stream: str, consumer: str, cap: _Capacity, spawn: Spawn) -> None:
    # startup only: reclaim whatever a previous (crashed) run left pending and reprocess it (idempotent),
    # bounded by capacity so a large pending set is drained gradually instead of pulled in all at once
    redis = get_redis()
    cursor = b"0-0"
    while True:
        if cap.free() <= 0:
            await cap.wait_free()
            continue
        cursor, claimed, _ = await redis.xautoclaim(
            stream, GROUP, consumer, min_idle_time=0, start_id=cursor, count=min(RECLAIM_BATCH, cap.free())
        )
        for msg_id, raw in claimed:
            cap.take()
            spawn(msg_id.decode(), raw)
        if cursor == b"0-0":
            return


async def _pump(stream: str, consumer: str, cap: _Capacity, spawn: Spawn) -> None:
    redis = get_redis()
    while True:
        if cap.free() <= 0:
            await cap.wait_free()  # no slot → block here instead of claiming more messages into RAM
            continue
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: ">"}, count=cap.free(), block=BLOCK_MS)
        for msg_id, raw in fresh[0][1] if fresh else []:
            cap.take()
            spawn(msg_id.decode(), raw)


async def _drive(stream: str, cap: _Capacity, spawn: Spawn) -> None:
    await ensure_group(stream)
    consumer = f"{stream}-{get_settings().worker_id}"
    logger.info("consuming %s concurrency=%d", stream, cap.limit)
    await _recover(stream, consumer, cap, spawn)
    await _pump(stream, consumer, cap, spawn)


# ---- per-job work: run the handler as a sub-task, settle on success, requeue/giveup on failure ----------
async def _normalize_job(cap: _Capacity, profiles: asyncio.Queue[str], msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_INGEST
    profile = await profiles.get()  # one dedicated libreoffice profile per in-flight normalize job
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
        cap.release()


async def _paginate_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_NORMALIZED
    try:
        fields = {k.decode(): v.decode() for k, v in raw.items() if k != b"data"}
        work = asyncio.create_task(handle_paginate(fields, raw.get(b"data", b"")))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("paginate failed file=%s\n%s", fields.get("filename", "?"), _tb(work.exception()))
        await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "paginate"))
    finally:
        cap.release()


async def _ocr_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_PAGES
    try:
        fields = {k.decode(): v.decode() for k, v in raw.items() if k != b"image"}
        work = asyncio.create_task(handle_ocr(fields, raw.get(b"image", b"")))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("ocr failed page=%s\n%s", fields.get("page_idx", "?"), _tb(work.exception()))
        await _retry_or_fail(stream, msg_id, raw, lambda: fail_page(fields["doc_id"], int(fields["page_idx"])))
    finally:
        cap.release()


async def _gapfill_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_GAPFILL
    try:
        fields = {k.decode(): v.decode() for k, v in raw.items() if k != b"image"}
        work = asyncio.create_task(handle_gapfill(fields, raw.get(b"image", b"")))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("gapfill failed page=%s\n%s", fields.get("page_idx", "?"), _tb(work.exception()))
        # retries exhausted → finalize with the VLM blocks alone so a RapidOCR error never loses the page
        await _retry_or_fail(stream, msg_id, raw, lambda: emit_vlm_only(fields))
    finally:
        cap.release()


async def _merge_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_MERGE
    try:
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        work = asyncio.create_task(handle_merge(fields))
        await asyncio.wait({work})
        if work.exception() is not None:
            logger.error("merge failed doc=%s\n%s", fields.get("doc_id", "?"), _tb(work.exception()))
            await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "merge"))
            return
        await _settle(stream, msg_id)
        await cleanup(fields["doc_id"])  # only after the merge message is acked → safe to delete blocks
    finally:
        cap.release()


async def _tabular_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_TABLES
    try:
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        work = asyncio.create_task(handle_tabular(fields))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("tabular failed doc=%s\n%s", fields.get("doc_id", "?"), _tb(work.exception()))
        await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "tabular"))
    finally:
        cap.release()


# ---- normalize: read `ingest`, convert, write `normalized`. one dedicated libreoffice profile per job
async def normalize() -> None:
    cap = _Capacity(NORMALIZE_CONCURRENCY)
    profiles = make_profile_pool(cap.limit)
    await _drive(STREAM_INGEST, cap, lambda mid, raw: _spawn(_normalize_job(cap, profiles, mid, raw)))


# ---- paginate: read `normalized`, render pages, write `pages`. dedicated pdfium per process-pool worker
async def paginate() -> None:
    cap = _Capacity(PAGINATE_CONCURRENCY)
    await _drive(STREAM_NORMALIZED, cap, lambda mid, raw: _spawn(_paginate_job(cap, mid, raw)))


# ---- ocr: read `pages`, extract via vLLM, write blocks + `merge`. one global concurrency bound ----------
async def ocr() -> None:
    await asyncio.to_thread(get_mineru_client)  # build the http client once now, not lazily mid-OCR
    cap = _Capacity(OCR_CONCURRENCY)
    await _drive(STREAM_PAGES, cap, lambda mid, raw: _spawn(_ocr_job(cap, mid, raw)))


# ---- gapfill: read `gapfill`, RapidOCR the lines the VLM dropped, finalize the page. CPU-bound, bounded low ----
async def gapfill() -> None:
    cap = _Capacity(RAPIDOCR_CONCURRENCY)
    await _drive(STREAM_GAPFILL, cap, lambda mid, raw: _spawn(_gapfill_job(cap, mid, raw)))


# ---- merge: read `merge`, assemble result.json, clean up. light concurrency ----------------------------
async def merge() -> None:
    cap = _Capacity(MERGE_CONCURRENCY)
    await _drive(STREAM_MERGE, cap, lambda mid, raw: _spawn(_merge_job(cap, mid, raw)))


# ---- tabular: read `tables`, SLM structure + describe, write tables. bounded by the SLM slot ------------
async def tabular() -> None:
    cap = _Capacity(SLM_CONCURRENCY)
    await _drive(STREAM_TABLES, cap, lambda mid, raw: _spawn(_tabular_job(cap, mid, raw)))


async def _main() -> None:
    await asyncio.gather(normalize(), paginate(), ocr(), gapfill(), merge(), tabular())


def main() -> None:
    configure_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
