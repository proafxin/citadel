import asyncio
import logging
import signal
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from citadel.db import get_engine
from citadel.services.ingestion import (
    GROUP,
    MAX_ATTEMPTS,
    PAGINATE_CONCURRENCY,
    RAPIDOCR_CONCURRENCY,
    RENDER_CONCURRENCY,
    STREAM_GAPFILL,
    STREAM_INGEST,
    STREAM_MERGE,
    STREAM_NORMALIZED,
    STREAM_PAGES,
    STREAM_RENDER,
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
    handle_render,
    handle_tabular,
    make_profile_pool,
    reap_orphan_blobs,
    requeue_message,
    shutdown,
)
from citadel.tabular.infer import HEADER_WORKERS
from config import CPU_QUARTER, configure_logging, get_settings

logger = logging.getLogger(__name__)

NORMALIZE_CONCURRENCY = CPU_QUARTER  # one isolated libreoffice profile per worker (per-job soffice)
OCR_CONCURRENCY = 192  # pages in flight; matched to the mineru client-pool sockets and vLLM --max-num-seqs. GPU saturates below 256 (sm~100%), so 192 holds throughput for ~1GB less host RAM
MERGE_CONCURRENCY = 4  # light assembly
PAGES_BUFFER = 32  # K: rendered pages kept buffered ahead of OCR so neither render nor OCR starves
# render is gated so `pages` holds at most OCR_CONCURRENCY (claimed/in-flight) + PAGES_BUFFER images — bounds the
# page-image RAM in Redis. without it a huge PDF renders ALL N pages up front (800 pages → GBs of images) → OOM.
PAGES_BOUND = OCR_CONCURRENCY + PAGES_BUFFER
GAPFILL_BUFFER = 64  # scanned pages OCR may run ahead of RapidOCR gap-fill before it backpressures (keeps the GPU
# busy while still bounding the scanned-page images buffered in `gapfill`); born-digital pages never enter it
GAPFILL_BOUND = RAPIDOCR_CONCURRENCY + GAPFILL_BUFFER

BLOCK_MS = 5000

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


async def _decode_or_settle(stream: str, msg_id: str, raw: dict[bytes, bytes]) -> dict[str, str] | None:
    # a structurally-unprocessable (non-UTF-8) entry can never be handled: drop it so it doesn't stay pending and
    # re-crash on every recovery. the binary image field is carried separately, so it is excluded from the decode.
    try:
        return {key.decode(): value.decode() for key, value in raw.items() if key != b"image"}
    except UnicodeDecodeError:
        logger.exception("undecodable entry stream=%s id=%s → dropped", stream, msg_id)
        await _settle(stream, msg_id)
        return None


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
    attempt = int(raw.get(b"attempt", b"0")) + 1
    if attempt > MAX_ATTEMPTS:
        await giveup()  # idempotent; if we crash before the settle below, recovery re-runs it then settles
        await _settle(stream, msg_id)
    else:
        await requeue_message(stream, msg_id, {**raw, b"attempt": str(attempt).encode()})


async def _recover(stream: str, consumer: str, cap: _Capacity, spawn: Spawn) -> None:
    # startup only: reprocess THIS consumer's own delivered-but-unacked entries, orphaned when a previous run crashed.
    # id="0" reads only our own pending — never another live consumer's in-flight — so there is no idle threshold to
    # guess and no risk of stealing a job that is legitimately still running. handlers are idempotent, so re-reading
    # one that had actually completed is a safe no-op. paginate by advancing past each batch (entries ack async).
    redis = get_redis()
    last = "0"
    while True:
        if cap.free() <= 0:
            await cap.wait_free()
            continue
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: last}, count=cap.free())
        entries = fresh[0][1] if fresh else []
        if not entries:
            return
        for msg_id, raw in entries:
            cap.take()
            spawn(msg_id.decode(), raw)
        last = entries[-1][0].decode()


async def _pump(
    stream: str, consumer: str, cap: _Capacity, spawn: Spawn, downstream: Callable[[], Awaitable[int]] | None = None
) -> None:
    redis = get_redis()
    while True:
        if cap.free() <= 0:
            await cap.wait_free()  # no slot → block here instead of claiming more messages into RAM
            continue
        room = cap.free()
        if downstream is not None:
            # backpressure: never produce more than the downstream stream can hold (bounds its image RAM)
            room = min(room, await downstream())
            if room <= 0:
                await asyncio.sleep(0.1)
                continue
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: ">"}, count=room, block=BLOCK_MS)
        for msg_id, raw in fresh[0][1] if fresh else []:
            cap.take()
            spawn(msg_id.decode(), raw)


async def _drive(
    stream: str, cap: _Capacity, spawn: Spawn, downstream: Callable[[], Awaitable[int]] | None = None
) -> None:
    await ensure_group(stream)
    consumer = f"{stream}-{get_settings().worker_id}"
    logger.info("consuming %s concurrency=%d", stream, cap.limit)
    await _recover(stream, consumer, cap, spawn)
    await _pump(stream, consumer, cap, spawn, downstream)


# ---- per-job work: run the handler as a sub-task, settle on success, requeue/giveup on failure ----------
async def _normalize_job(cap: _Capacity, profiles: asyncio.Queue[str], msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_INGEST
    profile = await profiles.get()  # one dedicated libreoffice profile per in-flight normalize job
    try:
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
        work = asyncio.create_task(handle_normalize(fields, profile))
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
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
        work = asyncio.create_task(handle_paginate(fields))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("paginate failed file=%s\n%s", fields.get("filename", "?"), _tb(work.exception()))
        await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "paginate"))
    finally:
        cap.release()


async def _render_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_RENDER
    try:
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
        work = asyncio.create_task(handle_render(fields))
        await asyncio.wait({work})
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("render failed page=%s\n%s", fields.get("page_idx", "?"), _tb(work.exception()))
        await _retry_or_fail(stream, msg_id, raw, lambda: fail_page(fields["doc_id"], int(fields["page_idx"])))
    finally:
        cap.release()


async def _ocr_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_PAGES
    try:
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
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
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
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
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
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
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
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


# ---- paginate: read `normalized`, count pages, emit one `render` job per page (markup/tabular resolved inline)
async def paginate() -> None:
    cap = _Capacity(PAGINATE_CONCURRENCY)
    await _drive(STREAM_NORMALIZED, cap, lambda mid, raw: _spawn(_paginate_job(cap, mid, raw)))


async def _pages_room() -> int:
    # backpressure gate for render: how many more page images `pages` can hold before OCR is behind
    return PAGES_BOUND - await get_redis().xlen(STREAM_PAGES)


# ---- render: read `render`, render one PDF page via the pdfium pool, write `pages`. bounded to the pool width and
# gated on `pages` depth so render never runs ahead of OCR by more than PAGES_BUFFER images (RAM bound) -----------
async def render() -> None:
    cap = _Capacity(RENDER_CONCURRENCY)
    await _drive(STREAM_RENDER, cap, lambda mid, raw: _spawn(_render_job(cap, mid, raw)), _pages_room)


async def _gapfill_room() -> int:
    # backpressure gate for ocr: how many more scanned-page images `gapfill` can hold before RapidOCR is behind
    return GAPFILL_BOUND - await get_redis().xlen(STREAM_GAPFILL)


# ---- ocr: read `pages`, extract via vLLM, write blocks + `merge`. one global concurrency bound, gated on `gapfill`
# depth so a scanned-heavy doc can't pile up gap-fill page images faster than RapidOCR drains them -----------------
async def ocr() -> None:
    await asyncio.to_thread(get_mineru_client)  # build the pooled clients once now, not lazily mid-OCR
    cap = _Capacity(OCR_CONCURRENCY)
    await _drive(STREAM_PAGES, cap, lambda mid, raw: _spawn(_ocr_job(cap, mid, raw)), _gapfill_room)


# ---- gapfill: read `gapfill`, RapidOCR the lines the VLM dropped, finalize the page. CPU-bound, bounded low ----
async def gapfill() -> None:
    cap = _Capacity(RAPIDOCR_CONCURRENCY)
    await _drive(STREAM_GAPFILL, cap, lambda mid, raw: _spawn(_gapfill_job(cap, mid, raw)))


# ---- merge: read `merge`, assemble result.json, clean up. light concurrency ----------------------------
async def merge() -> None:
    cap = _Capacity(MERGE_CONCURRENCY)
    await _drive(STREAM_MERGE, cap, lambda mid, raw: _spawn(_merge_job(cap, mid, raw)))


# ---- tabular: read `tables`, model header-detection + describe, write tables. bounded by the header pool -----
async def tabular() -> None:
    cap = _Capacity(HEADER_WORKERS)
    await _drive(STREAM_TABLES, cap, lambda mid, raw: _spawn(_tabular_job(cap, mid, raw)))


async def _main() -> None:
    await reap_orphan_blobs()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    stages = (normalize, paginate, render, ocr, gapfill, merge, tabular)
    consumers = [asyncio.create_task(stage()) for stage in stages]
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait([stop_task, *consumers], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (stop_task, *consumers):
            task.cancel()
        await asyncio.gather(stop_task, *consumers, return_exceptions=True)
        await shutdown()
        await get_engine().dispose()


def main() -> None:
    configure_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
