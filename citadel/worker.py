import asyncio
import logging
import signal
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from citadel.db import get_engine
from citadel.services.ingestion import (
    CROP_BOUND,
    DECODE_CONCURRENCY,
    GROUP,
    MAX_ATTEMPTS,
    PAGINATE_CONCURRENCY,
    RENDER_CONCURRENCY,
    STREAM_INGEST,
    STREAM_MERGE,
    STREAM_NORMALIZED,
    STREAM_PAGES,
    STREAM_RENDER,
    STREAM_TABLES,
    cleanup,
    ensure_group,
    fail_document,
    fail_page,
    get_crop_budget,
    get_redis,
    handle_merge,
    handle_normalize,
    handle_ocr,
    handle_paginate,
    handle_render,
    handle_tabular,
    make_profile_pool,
    reap_orphan_blobs,
    release_idle,
    requeue_message,
    shutdown,
)
from citadel.tabular.infer import HEADER_WORKERS
from config import CPU_EIGHTH, configure_logging, get_settings

logger = logging.getLogger(__name__)

NORMALIZE_CONCURRENCY = CPU_EIGHTH
MERGE_CONCURRENCY = CPU_EIGHTH  # light assembly
PAGES_BUFFER = DECODE_CONCURRENCY  # rendered pages sitting UNCLAIMED in `pages`, waiting for ocr to pick them up.
# sized to fill the decode gate in one claim: shallower and ocr takes what is there, leaves decode slots idle, and
# waits on render to catch up — a stall the GPU pays for. it does NOT bound pages in flight (ocr claims as fast as the
# crop budget allows), so it is purely a render-ahead buffer and purely a RAM bound (~2MB an image), deep enough that
# ocr never waits on pdfium and shallow enough that an 800-page PDF cannot rasterize itself into redis.
PAGES_IN_FLIGHT = 128  # pages CLAIMED at once — each pins its ~2MB encoded image in redis AND here (~4MB apiece).
# NOT tied to CROP_CONCURRENCY, though it used to be: they bound different things and the coupling made one unraisable
# without paying for the other. this is a RAM bound; the crop semaphore is a MODEL-saturation bound. going 128 -> 160
# with the semaphore cost 3GB of host memory and fed the model nothing.
# raising it does NOT feed the model: measured, doubling it tripled the queue on our own semaphore (crop_wait 260k ->
# 772k seconds on the scanned book) while the model did the SAME amount of work (predict unchanged at ~42k) and wall
# time got WORSE (799s -> 844s). the crops were never short of supply — a page sitting in crop_wait is a page whose
# crops are already cut and DEMANDING slots, so the semaphore is saturated long before the page cap binds. every page
# above this is a pinned image lengthening a queue that is already full.
# it must exist at all: a page has to be claimed before it can lay out, and the crop budget cannot see it until it
# does, so without a cap admission never blocks and claimed pages grow without end (measured: ~1700 claimed, ~3.4GB).

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


async def _recover(stream: str, consumer: str, cap: _Capacity | None, spawn: Spawn) -> None:
    # startup only: reprocess THIS consumer's own delivered-but-unacked entries, orphaned when a previous run crashed.
    # id="0" reads only our own pending — never another live consumer's in-flight — so there is no idle threshold to
    # guess and no risk of stealing a job that is legitimately still running. handlers are idempotent, so re-reading
    # one that had actually completed is a safe no-op. paginate by advancing past each batch (entries ack async).
    redis = get_redis()
    last = "0"
    while True:
        if cap is not None and cap.free() <= 0:
            await cap.wait_free()
            continue
        count = cap.free() if cap is not None else CROP_BOUND
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: last}, count=count)
        entries = fresh[0][1] if fresh else []
        if not entries:
            return
        for msg_id, raw in entries:
            if cap is not None:
                cap.take()
            spawn(msg_id.decode(), raw)
        last = entries[-1][0].decode()


async def _pump(
    stream: str,
    consumer: str,
    cap: _Capacity | None,
    spawn: Spawn,
    downstream: Callable[[], Awaitable[int]] | None = None,
) -> None:
    redis = get_redis()
    while True:
        room = None
        if cap is not None:
            if cap.free() <= 0:
                await cap.wait_free()  # no slot → block here instead of claiming more messages into RAM
                continue
            room = cap.free()
        if downstream is not None:
            # backpressure: never produce more than the downstream stage can absorb (bounds its image RAM). a stage
            # with no cap of its own (ocr) is bounded ENTIRELY by this — the downstream resource is its only gate
            available = await downstream()
            room = available if room is None else min(room, available)
        if room is not None and room <= 0:
            await asyncio.sleep(0.1)
            continue
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: ">"}, count=room, block=BLOCK_MS)
        for msg_id, raw in fresh[0][1] if fresh else []:
            if cap is not None:
                cap.take()
            spawn(msg_id.decode(), raw)


async def _drive(
    stream: str, cap: _Capacity | None, spawn: Spawn, downstream: Callable[[], Awaitable[int]] | None = None
) -> None:
    await ensure_group(stream)
    consumer = f"{stream}-{get_settings().worker_id}"
    logger.info("consuming %s concurrency=%s", stream, cap.limit if cap is not None else "crop-gated")
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


async def _ocr_job(cap: _Capacity | None, msg_id: str, raw: dict[bytes, bytes]) -> None:
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
        if cap is not None:
            cap.release()


DRAINED_STREAMS = (
    STREAM_INGEST,
    STREAM_NORMALIZED,
    STREAM_RENDER,
    STREAM_PAGES,
    STREAM_MERGE,
    STREAM_TABLES,
)


async def _release_if_drained() -> None:
    # a merge is the last thing that touches a document, so the moment one is acked is the moment the pipeline MIGHT be
    # empty. an empty stream proves it: a claimed-but-unacked entry still counts in xlen, so xlen==0 across every stream
    # means nothing is queued AND nothing is in flight. that is a guarantee, not a guess — which is what makes it safe
    # to stop the pools, since stopping one with a task still running would kill it mid-render. they rebuild lazily as a
    # fork off the forkserver on the next upload.
    redis = get_redis()
    for stream in DRAINED_STREAMS:
        if await redis.xlen(stream):
            return
    await asyncio.to_thread(release_idle)
    logger.info("pipeline drained → released process pools")


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
        await _release_if_drained()
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
    # gate render on the pages nobody has picked up yet — NOT on xlen. a claimed entry stays in the stream, unacked,
    # for the whole life of its page (decode, layout, crops, the entire VLM wait), so gating on xlen silently caps
    # pages IN FLIGHT rather than the render-ahead buffer, and throttles the model it is supposed to be feeding.
    # what render must not do is run away — an 800-page PDF rasterized up front is GBs of images — and that is a
    # question about the buffer alone. ocr claims from it as fast as the crop budget allows.
    redis = get_redis()
    claimed = int((await redis.xpending(STREAM_PAGES, GROUP))["pending"])
    unclaimed = await redis.xlen(STREAM_PAGES) - claimed
    return PAGES_BUFFER - unclaimed


# ---- render: read `render`, render one PDF page via the pdfium pool, write `pages`. bounded to the pool width and
# gated on the UNCLAIMED depth of `pages`, so it keeps a shallow buffer ready without bounding what ocr may hold ----
async def render() -> None:
    await ensure_group(STREAM_PAGES)  # our gate reads `pages`' pending list, and ocr — which owns that group — may not
    # have created it yet. XPENDING on a missing group is an error, not an empty answer. idempotent.
    cap = _Capacity(RENDER_CONCURRENCY)
    await _drive(STREAM_RENDER, cap, lambda mid, raw: _spawn(_render_job(cap, mid, raw)), _pages_room)


async def _ocr_room() -> int:
    # crops are the bottleneck, so admission is gated on them — but the crop budget CANNOT see a page until the
    # detector has found its regions, and a page must be claimed to be detected. so a claimed page still waiting on the
    # detector has charged nothing, the budget reads free, and this gate would keep claiming forever: every one of
    # those pages holds its encoded image, and they pile up unbounded. PAGES_IN_FLIGHT closes that hole.
    return get_crop_budget().free()


# ---- ocr: read `pages`, extract via vLLM, write blocks + `merge`. crops decide how much work is in flight; this cap
# only bounds the encoded images we hold while it happens, and is set far above what the crop budget will ever admit
# (a page yields at least one crop, and only ~2 on a born-digital page) so it can never throttle the model ------------
async def ocr() -> None:
    cap = _Capacity(PAGES_IN_FLIGHT)
    await _drive(STREAM_PAGES, cap, lambda mid, raw: _spawn(_ocr_job(cap, mid, raw)), _ocr_room)


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
    stages = (normalize, paginate, render, ocr, merge, tabular)
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
