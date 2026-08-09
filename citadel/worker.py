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
from citadel.services.batching import STREAM_BATCH, record_summary, summarize_document
from citadel.services.ingestion import (
    BULK_READ_COUNT,
    GROUP,
    MAX_ATTEMPTS,
    PAGINATE_CONCURRENCY,
    RENDER_CONCURRENCY,
    RENDER_DOCS,
    STREAM_INGEST,
    STREAM_MERGE,
    STREAM_NORMALIZED,
    STREAM_PAGES,
    STREAM_RASTERIZE,
    STREAM_STRUCTURE,
    STREAM_TABLE_STRUCTURE,
    cleanup,
    ensure_group,
    fail_document,
    fail_page,
    handle_merge,
    handle_normalize,
    handle_ocr,
    handle_paginate,
    handle_rasterize,
    handle_render,
    handle_structure,
    handle_table_structure,
    make_profile_pool,
    page_image_key,
    reap_orphan_blobs,
    release_idle,
    render_stream,
    requeue_message,
    shutdown,
)
from citadel.services.query import STREAM_RESOLVE_BATCH, record_resolve_batch, resolve_batch_job
from citadel.services.slm import read_replies
from config import CPU_EIGHTH, configure_logging, get_settings

logger = logging.getLogger(__name__)

NORMALIZE_CONCURRENCY = 4
MERGE_CONCURRENCY = CPU_EIGHTH
STRUCTURE_CONCURRENCY = CPU_EIGHTH
OCR_CONCURRENCY = 160
VISION_BUFFER = OCR_CONCURRENCY

_tasks: set[asyncio.Task[None]] = set()

Spawn = Callable[[str, dict[bytes, bytes]], None]


@dataclass
class _Capacity:
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


@lru_cache
def get_vision_capacity() -> _Capacity:
    return _Capacity(OCR_CONCURRENCY + VISION_BUFFER)


def _tb(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


async def _settle(stream: str, msg_id: str) -> None:
    redis = get_redis()
    await redis.xack(stream, GROUP, msg_id)
    await redis.xdel(stream, msg_id)


async def _decode_or_settle(stream: str, msg_id: str, raw: dict[bytes, bytes]) -> dict[str, str] | None:
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
    _tasks.add(task)
    task.add_done_callback(_done)


async def _run_cancelable(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    work = asyncio.create_task(coro)
    try:
        await asyncio.wait({work})
    except asyncio.CancelledError:
        work.cancel()
        await asyncio.wait({work})
        raise
    return work


async def _retry_or_fail(
    stream: str, msg_id: str, raw: dict[bytes, bytes], giveup: Callable[[], Awaitable[None]]
) -> bool:
    attempt = int(raw.get(b"attempt", b"0")) + 1
    if attempt > MAX_ATTEMPTS:
        await giveup()
        await _settle(stream, msg_id)
        return True
    await requeue_message(stream, msg_id, {**raw, b"attempt": str(attempt).encode()})
    return False


async def _recover(stream: str, consumer: str, cap: _Capacity | None, spawn: Spawn) -> None:
    redis = get_redis()
    last = "0"
    while True:
        if cap is not None and cap.free() <= 0:
            await cap.wait_free()
            continue
        count = cap.free() if cap is not None else BULK_READ_COUNT
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
                await cap.wait_free()
                continue
            room = cap.free()
        if downstream is not None:
            available = await downstream()
            room = available if room is None else min(room, available)
        if room is not None and room <= 0:
            await asyncio.sleep(0.1)
            continue
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: ">"}, count=room, block=0)
        for msg_id, raw in fresh[0][1] if fresh else []:
            if cap is not None:
                cap.take()
            spawn(msg_id.decode(), raw)


async def _drive(
    stream: str, cap: _Capacity | None, spawn: Spawn, downstream: Callable[[], Awaitable[int]] | None = None
) -> None:
    await ensure_group(stream)
    consumer = f"{stream}-{get_settings().worker_id}"
    logger.info("consuming %s concurrency=%s", stream, cap.limit if cap is not None else "unbounded")
    await _recover(stream, consumer, cap, spawn)
    await _pump(stream, consumer, cap, spawn, downstream)


async def _normalize_job(cap: _Capacity, profiles: asyncio.Queue[str], msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_INGEST
    profile = await profiles.get()
    try:
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
        work = await _run_cancelable(handle_normalize(fields, profile))
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
        work = await _run_cancelable(handle_paginate(fields))
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("paginate failed file=%s\n%s", fields.get("filename", "?"), _tb(work.exception()))
        await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "paginate"))
    finally:
        cap.release()


async def _render_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = render_stream(raw[b"doc_id"].decode())
    try:
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
        work = await _run_cancelable(handle_render(fields))
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("render failed page=%s\n%s", fields.get("page_idx", "?"), _tb(work.exception()))
        await _retry_or_fail(stream, msg_id, raw, lambda: fail_page(fields["doc_id"], int(fields["page_idx"])))
    finally:
        cap.release()


async def _rasterize_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_RASTERIZE
    fields = await _decode_or_settle(stream, msg_id, raw)
    if fields is None:
        cap.release()
        return
    work = await _run_cancelable(handle_rasterize(fields))
    if work.exception() is None:
        await _settle(stream, msg_id)
        return
    cap.release()
    logger.error("rasterize failed page=%s\n%s", fields.get("page_idx", "?"), _tb(work.exception()))
    await _retry_or_fail(stream, msg_id, raw, lambda: fail_page(fields["doc_id"], int(fields["page_idx"])))


async def _ocr_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_PAGES
    terminal = True
    fields: dict[str, str] | None = None
    try:
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
        work = await _run_cancelable(handle_ocr(fields))
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("ocr failed page=%s\n%s", fields.get("page_idx", "?"), _tb(work.exception()))
        terminal = await _retry_or_fail(
            stream, msg_id, raw, lambda: fail_page(fields["doc_id"], int(fields["page_idx"]))
        )
    finally:
        cap.release()
        if terminal:
            get_vision_capacity().release()
            if fields is not None:
                await get_redis().delete(page_image_key(fields["doc_id"], int(fields["page_idx"])))


DRAINED_STREAMS = (
    STREAM_INGEST,
    STREAM_NORMALIZED,
    STREAM_RASTERIZE,
    STREAM_PAGES,
    STREAM_STRUCTURE,
    STREAM_TABLE_STRUCTURE,
    STREAM_MERGE,
)


async def _release_if_drained() -> None:
    redis = get_redis()
    for stream in DRAINED_STREAMS:
        if await redis.xlen(stream):
            return
    if await redis.scard(RENDER_DOCS):
        return
    await asyncio.to_thread(release_idle)
    logger.info("pipeline drained → released process pools")


async def _merge_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_MERGE
    try:
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
        work = await _run_cancelable(handle_merge(fields))
        if work.exception() is not None:
            logger.error("merge failed doc=%s\n%s", fields.get("doc_id", "?"), _tb(work.exception()))
            await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "merge"))
            return
        await _settle(stream, msg_id)
        await cleanup(fields["doc_id"])
        await _release_if_drained()
    finally:
        cap.release()


async def _structure_job(cap: _Capacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_STRUCTURE
    try:
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
        work = await _run_cancelable(handle_structure(fields))
        error = work.exception()
        if error is None:
            await _settle(stream, msg_id)
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
    work = await _run_cancelable(handle_table_structure(fields))
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
    work = await _run_cancelable(summarize_document(fields))
    error = work.exception()
    if error is None:
        await _settle(stream, msg_id)
    else:
        logger.error("batch failed doc=%s\n%s", fields.get("doc_id", "?"), _tb(error))
        await _retry_or_fail(stream, msg_id, raw, lambda: fail_document(fields.get("doc_id", ""), "batch"))
    await record_summary(int(fields["library_id"]))


async def _resolve_batch_job(msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_RESOLVE_BATCH
    fields = await _decode_or_settle(stream, msg_id, raw)
    if fields is None:
        return
    work = await _run_cancelable(resolve_batch_job(fields))
    error = work.exception()
    if error is None:
        await _settle(stream, msg_id)
        await record_resolve_batch(int(fields["library_id"]))
    else:
        logger.error(
            "resolve batch failed library=%s batch=%s\n%s",
            fields.get("library_id", "?"),
            fields.get("batch_no", "?"),
            _tb(error),
        )
        await _retry_or_fail(stream, msg_id, raw, lambda: record_resolve_batch(int(fields["library_id"])))


async def normalize() -> None:
    cap = _Capacity(NORMALIZE_CONCURRENCY)
    profiles = make_profile_pool(cap.limit)
    await _drive(STREAM_INGEST, cap, lambda mid, raw: _spawn(_normalize_job(cap, profiles, mid, raw)))


async def paginate() -> None:
    cap = _Capacity(PAGINATE_CONCURRENCY)
    await _drive(STREAM_NORMALIZED, cap, lambda mid, raw: _spawn(_paginate_job(cap, mid, raw)))


async def _claim_ready(consumer: str, cap: _Capacity, spawn: Spawn, room: int) -> int:
    redis = get_redis()
    docs = sorted(name.decode() for name in cast("set[bytes]", await redis.smembers(RENDER_DOCS)))
    if not docs:
        return 0
    active = docs[:room]
    for doc_id in active:
        await ensure_group(render_stream(doc_id))
    streams = {render_stream(doc_id): ">" for doc_id in active}
    fresh = cast(
        "list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]",
        await redis.xreadgroup(GROUP, consumer, streams, count=1),
    )
    returned = {name.decode(): entries for name, entries in fresh}
    claimed = 0
    for doc_id in active:
        entries = returned.get(render_stream(doc_id), [])
        if not entries:
            if not await redis.xlen(render_stream(doc_id)):
                await redis.srem(RENDER_DOCS, doc_id)
            continue
        for msg_id, raw in entries:
            cap.take()
            spawn(msg_id.decode(), raw)
            claimed += 1
    return claimed


async def _recover_render(consumer: str, cap: _Capacity, spawn: Spawn) -> None:
    redis = get_redis()
    for name in cast("set[bytes]", await redis.smembers(RENDER_DOCS)):
        stream = render_stream(name.decode())
        await ensure_group(stream)
        await _recover(stream, consumer, cap, spawn)


async def render() -> None:
    await ensure_group(STREAM_PAGES)
    cap = _Capacity(RENDER_CONCURRENCY)
    consumer = f"render-{get_settings().worker_id}"

    def spawn(mid, raw):
        return _spawn(_render_job(cap, mid, raw))

    await _recover_render(consumer, cap, spawn)
    while True:
        if cap.free() <= 0:
            await cap.wait_free()
            continue
        room = cap.free()
        if room <= 0 or not await _claim_ready(consumer, cap, spawn, room):
            await asyncio.sleep(0.1)


async def rasterize() -> None:
    cap = get_vision_capacity()
    await _drive(STREAM_RASTERIZE, cap, lambda mid, raw: _spawn(_rasterize_job(cap, mid, raw)))


async def ocr() -> None:
    cap = _Capacity(OCR_CONCURRENCY)
    await _drive(STREAM_PAGES, cap, lambda mid, raw: _spawn(_ocr_job(cap, mid, raw)))


async def merge() -> None:
    cap = _Capacity(MERGE_CONCURRENCY)
    await _drive(STREAM_MERGE, cap, lambda mid, raw: _spawn(_merge_job(cap, mid, raw)))


async def structure() -> None:
    cap = _Capacity(STRUCTURE_CONCURRENCY)
    await _drive(STREAM_STRUCTURE, cap, lambda mid, raw: _spawn(_structure_job(cap, mid, raw)))


async def table_structure() -> None:
    await _drive(STREAM_TABLE_STRUCTURE, None, lambda mid, raw: _spawn(_table_structure_job(mid, raw)))


async def batch() -> None:
    await _drive(STREAM_BATCH, None, lambda mid, raw: _spawn(_batch_job(mid, raw)))


async def resolve_batches() -> None:
    await _drive(STREAM_RESOLVE_BATCH, None, lambda mid, raw: _spawn(_resolve_batch_job(mid, raw)))


async def _main() -> None:
    await reap_orphan_blobs()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    stages = (normalize, paginate, render, rasterize, ocr, structure, table_structure, merge, batch, resolve_batches)
    consumers = [asyncio.create_task(stage()) for stage in stages]
    consumers.append(asyncio.create_task(read_replies()))
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait([stop_task, *consumers], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (stop_task, *consumers):
            task.cancel()
        jobs = list(_tasks)
        for task in jobs:
            task.cancel()
        await asyncio.gather(stop_task, *consumers, *jobs, return_exceptions=True)
        await shutdown()
        await get_engine().dispose()


def main() -> None:
    configure_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
