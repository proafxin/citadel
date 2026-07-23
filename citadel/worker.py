import asyncio
import logging
import signal
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, cast

from citadel.bus import get_redis
from citadel.db import get_engine
from citadel.services.batching import STREAM_BATCH, summarize_batch
from citadel.services.ingestion import (
    CROP_BOUND,
    DECODE_CONCURRENCY,
    GROUP,
    MAX_ATTEMPTS,
    PAGINATE_CONCURRENCY,
    RENDER_CONCURRENCY,
    RENDER_DOCS,
    STREAM_INGEST,
    STREAM_MERGE,
    STREAM_NORMALIZED,
    STREAM_PAGES,
    STREAM_STRUCTURE,
    STREAM_TABLE_STRUCTURE,
    cleanup,
    ensure_group,
    fail_document,
    fail_page,
    get_admission,
    get_crop_budget,
    handle_merge,
    handle_normalize,
    handle_ocr,
    handle_paginate,
    handle_render,
    handle_structure,
    handle_table_structure,
    log_group_closes,
    log_timeline,
    make_profile_pool,
    reap_orphan_blobs,
    release_idle,
    render_stream,
    render_weights,
    requeue_message,
    sample_timeline,
    shutdown,
)
from citadel.services.paddle import log_crop_sizes
from citadel.services.slm import read_replies
from config import CPU_EIGHTH, CPU_THIRD, configure_logging, get_settings

logger = logging.getLogger(__name__)

NORMALIZE_CONCURRENCY = CPU_THIRD  # see PAGINATE_CONCURRENCY: the ramp, not steady state
MERGE_CONCURRENCY = CPU_EIGHTH  # light assembly
STRUCTURE_CONCURRENCY = CPU_EIGHTH  # docs being PREPARED (stitch/reclassify) and routed at once — cpu only, no SLM
PAGES_BUFFER = DECODE_CONCURRENCY  # rendered pages sitting UNCLAIMED in `pages`, waiting for ocr to pick them up.
# sized to fill the decode gate in one claim: shallower and ocr takes what is there, leaves decode slots idle, and
# waits on render to catch up — a stall the GPU pays for. it does NOT bound pages in flight (ocr claims as fast as the
# crop budget allows), so it is purely a render-ahead buffer and purely a RAM bound (~2MB an image), deep enough that
# ocr never waits on pdfium and shallow enough that an 800-page PDF cannot rasterize itself into redis.
#
# there is no cap on pages IN FLIGHT any more, and its removal is the point. it was a fixed page count (96) standing in
# for a crop bound, and a page's crop yield spans 0 to 23.5 — so it was simultaneously too loose for a scanned book and
# too tight for a born-digital one, which could put only ~79 crops in front of 128 slots and then could claim no more,
# its pages held by crops still queueing. admission is now UNCHARGED_PAGES (the invisible window) plus the crop budget:
# both denominated in what the GPU consumes. the old measurement that raising the page cap hurt (799s -> 844s) still
# holds and is not contradicted — raising a cap uniformly deepens a queue that is already full, whereas admitting on
# crop supply adds pages only when the model would otherwise sit idle.

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
    stream = render_stream(raw[b"doc_id"].decode())  # settle against the document's OWN stream, never a shared one
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


async def _ocr_job(doc_id: str, page_idx: int, msg_id: str, raw: dict[bytes, bytes]) -> None:
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
        # normally already settled the moment the page charged its crops; this covers the paths that never got there
        # (undecodable entry, detector failure), so a page can never leak an admission slot
        get_admission().settle(doc_id, page_idx)


def _ocr_claim(msg_id: str, raw: dict[bytes, bytes]) -> None:
    # the slot is taken SYNCHRONOUSLY at claim, before the dispatch loop can read free() again — a page accounted for
    # only once its task started running would let the loop claim the whole stream in the gap
    doc_id = raw[b"doc_id"].decode()
    page_idx = int(raw[b"page_idx"])
    get_admission().enter(doc_id, page_idx)
    _spawn(_ocr_job(doc_id, page_idx, msg_id, raw))


DRAINED_STREAMS = (
    STREAM_INGEST,
    STREAM_NORMALIZED,
    STREAM_PAGES,
    STREAM_STRUCTURE,
    STREAM_TABLE_STRUCTURE,
    STREAM_MERGE,
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
    if await redis.scard(RENDER_DOCS):  # a doc leaves the rotation only at xlen 0, i.e. drained AND acked
        return
    await asyncio.to_thread(release_idle)
    log_crop_sizes()
    log_timeline()
    log_group_closes()
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


async def _structure_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_STRUCTURE
    try:
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
        work = asyncio.create_task(handle_structure(fields))
        await asyncio.wait({work})
        error = work.exception()
        if error is None:
            await _settle(stream, msg_id)  # merge was fired inside the handler; acking here just retires the job
            return
        logger.error("structure failed doc=%s\n%s", fields.get("doc_id", "?"), _tb(error))
        await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "structure"))
    finally:
        cap.release()


async def _table_structure_job(msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_TABLE_STRUCTURE
    fields = await _decode_or_settle(stream, msg_id, raw)
    if fields is None:
        return
    work = asyncio.create_task(handle_table_structure(fields))
    await asyncio.wait({work})
    error = work.exception()
    if error is None:
        await _settle(stream, msg_id)
        return
    logger.error("table_structure failed doc=%s\n%s", fields.get("doc_id", "?"), _tb(error))
    await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "table_structure"))


async def _batch_job(msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_BATCH
    fields = await _decode_or_settle(stream, msg_id, raw)
    if fields is None:
        return
    work = asyncio.create_task(summarize_batch(fields))
    await asyncio.wait({work})
    error = work.exception()
    if error is None:
        await _settle(stream, msg_id)
        return
    logger.error("batch failed doc=%s\n%s", fields.get("doc_id", "?"), _tb(error))
    await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "batch"))


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


@lru_cache
def get_render_credit() -> dict[str, float]:
    # a pass has RENDER_CONCURRENCY slots — three — so a document owed 1.4% of it rounds to zero and would never be
    # claimed at all, which is LPT by accident: the dominant document takes every slot and the rest starve until it
    # drains. credit ACCUMULATES across passes instead, so a small share is paid late rather than never, and the
    # long-run allocation is the proportional one even though no single pass can express it
    return {}


async def _claim_by_share(consumer: str, cap: _Capacity, spawn: Spawn, room: int) -> int:
    # each document gets a slice of this pass PROPORTIONAL to the crops it still owes, largest first so that when the
    # room is small it is the critical path that gets it. proportional is the whole point: equal share starves the
    # dominant document (measured, +5s) and no share at all starves the model (measured, a 70s collapse)
    redis = get_redis()
    docs = sorted(name.decode() for name in cast("set[bytes]", await redis.smembers(RENDER_DOCS)))
    if not docs:
        return 0
    weights = await render_weights(docs)
    total = sum(weights.values()) or 1.0
    credit = get_render_credit()
    for gone in set(credit) - set(docs):
        del credit[gone]  # a finished document must not carry credit back if its id is ever reused
    for doc_id in docs:
        credit[doc_id] = credit.get(doc_id, 0.0) + room * weights[doc_id] / total
    claimed = 0
    for doc_id in sorted(docs, key=lambda name: credit[name], reverse=True):
        if claimed >= room:
            break
        share = min(int(credit[doc_id]), room - claimed)
        if share <= 0:
            continue
        stream = render_stream(doc_id)
        await ensure_group(stream)
        fresh = cast(
            "list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]",
            await redis.xreadgroup(GROUP, consumer, {stream: ">"}, count=share),
        )
        entries = fresh[0][1] if fresh else []
        if not entries:
            # xlen counts a claimed-but-unacked entry too, so zero means drained AND finished — never merely quiet
            if not await redis.xlen(stream):
                await redis.srem(RENDER_DOCS, doc_id)
                credit.pop(doc_id, None)
            continue
        for msg_id, raw in entries:
            cap.take()
            spawn(msg_id.decode(), raw)
            claimed += 1
        credit[doc_id] -= len(entries)  # spend only what was actually taken; unclaimed credit rolls to the next pass
    return claimed


async def _recover_render(consumer: str, cap: _Capacity, spawn: Spawn) -> None:
    # startup only: this consumer's own delivered-but-unacked entries, orphaned by a previous crash, one doc at a time
    redis = get_redis()
    for name in cast("set[bytes]", await redis.smembers(RENDER_DOCS)):
        stream = render_stream(name.decode())
        await ensure_group(stream)
        await _recover(stream, consumer, cap, spawn)


# ---- render: read each document's OWN stream, sharing each pass in proportion to the crops it still owes, so the
# critical path runs at full rate and sparse documents fill what it leaves. gated on the UNCLAIMED depth of `pages` --
async def render() -> None:
    await ensure_group(STREAM_PAGES)  # our gate reads `pages`' pending list, and ocr — which owns that group — may not
    # have created it yet. XPENDING on a missing group is an error, not an empty answer. idempotent.
    cap = _Capacity(RENDER_CONCURRENCY)
    consumer = f"render-{get_settings().worker_id}"
    spawn: Spawn = lambda mid, raw: _spawn(_render_job(cap, mid, raw))  # ruff:ignore[lambda-assignment]
    await _recover_render(consumer, cap, spawn)
    while True:
        if cap.free() <= 0:
            await cap.wait_free()
            continue
        room = min(cap.free(), await _pages_room())
        if room <= 0 or not await _claim_by_share(consumer, cap, spawn, room):
            await asyncio.sleep(0.1)


async def _ocr_room() -> int:
    # admission is denominated in CROPS, the unit the GPU actually consumes — never in pages, whose crop yield varies
    # more than twentyfold across documents. the crop budget cannot see a page until the detector has found its
    # regions, so the uncharged window is bounded separately; past that a page is fully represented in crops and no
    # longer occupies a slot. so a sparse document keeps drawing pages until it has filled the model, and a dense one
    # stops after a handful, without either being told which it is
    return min(get_crop_budget().free(), get_admission().free())


# ---- ocr: read `pages`, extract via vLLM, write blocks + `merge`. crops decide how much work is in flight; this cap
# only bounds the encoded images we hold while it happens, and is set far above what the crop budget will ever admit
# (a page yields at least one crop, and only ~2 on a born-digital page) so it can never throttle the model ------------
async def ocr() -> None:
    await _drive(STREAM_PAGES, None, _ocr_claim, _ocr_room)


# ---- merge: read `merge`, assemble result.json, clean up. light concurrency ----------------------------
async def merge() -> None:
    cap = _Capacity(MERGE_CONCURRENCY)
    await _drive(STREAM_MERGE, cap, lambda mid, raw: _spawn(_merge_job(cap, mid, raw)))


# ---- structure: prepare each doc the moment its ocr finishes and route it — no tables goes straight to merge, tables
# go to the table_structure stage below
async def structure() -> None:
    cap = _Capacity(STRUCTURE_CONCURRENCY)
    await _drive(STREAM_STRUCTURE, cap, lambda mid, raw: _spawn(_structure_job(cap, mid, raw)))


# ---- table_structure: a plain stage between structure and merge. UNBOUNDED on our side — a unit does no heavy work
# of its own, it hands a job to the slm stream, so the batching and the concurrency are vllm's to decide. a client-side
# cap here can only starve the scheduler: measured at CPU_EIGHTH it pinned qwen to `Running: 3, Waiting: 0` all run
async def table_structure() -> None:
    await _drive(STREAM_TABLE_STRUCTURE, None, lambda mid, raw: _spawn(_table_structure_job(mid, raw)))


# ---- batch: read `batch`, one job per document batch. UNBOUNDED like table_structure — each job hands a summary to the
# slm stream and writes the reply back, so the batching and concurrency are vllm's to decide, not a client-side cap
async def batch() -> None:
    await _drive(STREAM_BATCH, None, lambda mid, raw: _spawn(_batch_job(mid, raw)))


async def _main() -> None:
    await reap_orphan_blobs()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    stages = (normalize, paginate, render, ocr, structure, table_structure, merge, batch)
    consumers = [asyncio.create_task(stage()) for stage in stages]
    consumers.extend((asyncio.create_task(read_replies()), asyncio.create_task(sample_timeline())))
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
