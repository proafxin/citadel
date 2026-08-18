import asyncio
import logging
import signal
import socket
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, cast

from citadel.bus import get_redis
from citadel.db import get_engine
from citadel.services.batching import STREAM_BATCH, mark_summary_failed, record_summary, summarize_document
from citadel.services.capacity import OCR_CONCURRENCY, GlobalCapacity, get_capacity, get_vision_capacity
from citadel.services.document import mark_library_failed, mark_library_ready
from citadel.services.ingestion import (
    BULK_READ_COUNT,
    GROUP,
    MAX_ATTEMPTS,
    PAGINATE_CONCURRENCY,
    RASTERIZE_PULL_CONCURRENCY,
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
from citadel.services.readiness import STREAM_LIBRARY_READY
from citadel.services.retrieval import STREAM_EMBED, handle_embed
from config import CPU_EIGHTH, configure_logging

logger = logging.getLogger(__name__)

NORMALIZE_CONCURRENCY = 4
MERGE_CONCURRENCY = CPU_EIGHTH
STRUCTURE_CONCURRENCY = CPU_EIGHTH

HOSTNAME = socket.gethostname()

_tasks: set[asyncio.Task[None]] = set()

Spawn = Callable[[str, dict[bytes, bytes]], None]


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


async def _recover(stream: str, consumer: str, cap: GlobalCapacity | None, spawn: Spawn) -> None:
    redis = get_redis()
    last = "0"
    while True:
        if cap is not None and await cap.free() <= 0:
            await cap.wait_free()
            continue
        count = await cap.free() if cap is not None else BULK_READ_COUNT
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: last}, count=count)
        entries = fresh[0][1] if fresh else []
        if not entries:
            return
        for msg_id, raw in entries:
            if cap is not None:
                await cap.take()
            spawn(msg_id.decode(), raw)
        last = entries[-1][0].decode()


async def _pump(
    stream: str,
    consumer: str,
    cap: GlobalCapacity | None,
    spawn: Spawn,
    downstream: Callable[[], Awaitable[int]] | None = None,
) -> None:
    redis = get_redis()
    while True:
        room = None
        if cap is not None:
            if await cap.free() <= 0:
                await cap.wait_free()
                continue
            room = await cap.free()
        if downstream is not None:
            available = await downstream()
            room = available if room is None else min(room, available)
        if room is not None and room <= 0:
            await asyncio.sleep(0.1)
            continue
        fresh = await redis.xreadgroup(GROUP, consumer, {stream: ">"}, count=room, block=0)
        for msg_id, raw in fresh[0][1] if fresh else []:
            if cap is not None:
                await cap.take()
            spawn(msg_id.decode(), raw)


async def _drive(
    stream: str, cap: GlobalCapacity | None, spawn: Spawn, downstream: Callable[[], Awaitable[int]] | None = None
) -> None:
    await ensure_group(stream)
    consumer = f"{stream}-{HOSTNAME}"
    logger.info("consuming %s concurrency=%s", stream, cap.limit if cap is not None else "unbounded")
    await _recover(stream, consumer, cap, spawn)
    await _pump(stream, consumer, cap, spawn, downstream)


async def _normalize_job(
    cap: GlobalCapacity, profiles: asyncio.Queue[str], msg_id: str, raw: dict[bytes, bytes]
) -> None:
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
        await cap.release()


async def _paginate_job(cap: GlobalCapacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
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
        await cap.release()


async def _render_job(cap: GlobalCapacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
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
        await cap.release()


def _vision_key(fields: dict[str, str]) -> str:
    return f"{fields['doc_id']}:{fields['page_idx']}"


async def _rasterize_job(cap: GlobalCapacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_RASTERIZE
    try:
        fields = await _decode_or_settle(stream, msg_id, raw)
        if fields is None:
            return
        await get_vision_capacity().acquire(_vision_key(fields))
        work = await _run_cancelable(handle_rasterize(fields))
        if work.exception() is None:
            await _settle(stream, msg_id)
            return
        logger.error("rasterize failed page=%s\n%s", fields.get("page_idx", "?"), _tb(work.exception()))
        terminal = await _retry_or_fail(
            stream, msg_id, raw, lambda: fail_page(fields["doc_id"], int(fields["page_idx"]))
        )
        if terminal:
            await get_vision_capacity().release(_vision_key(fields))
    finally:
        await cap.release()


async def _ocr_job(cap: GlobalCapacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
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
        await cap.release()
        if terminal and fields is not None:
            await get_vision_capacity().release(_vision_key(fields))
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


async def _merge_job(cap: GlobalCapacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
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
        await cap.release()


async def _structure_job(cap: GlobalCapacity, msg_id: str, raw: dict[bytes, bytes]) -> None:
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
        await cap.release()


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
        doc_id, library_id = int(fields.get("doc_id", "0")), int(fields.get("library_id", "0"))
        await _retry_or_fail(stream, msg_id, raw, lambda: mark_summary_failed(doc_id, library_id))
    await record_summary(int(fields["library_id"]))


async def _embed_giveup(library_id: str) -> None:
    logger.error("embedding permanently failed library=%s — marking library failed", library_id)
    await mark_library_failed(int(library_id))


async def _embed_job(msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_EMBED
    fields = await _decode_or_settle(stream, msg_id, raw)
    if fields is None:
        return
    work = await _run_cancelable(handle_embed(fields))
    if work.exception() is None:
        await _settle(stream, msg_id)
        return
    logger.error("embed failed library=%s\n%s", fields.get("library_id", "?"), _tb(work.exception()))
    await _retry_or_fail(stream, msg_id, raw, lambda: _embed_giveup(fields.get("library_id", "?")))


async def _library_ready_giveup(library_id: str) -> None:
    logger.error("marking library ready permanently failed library=%s", library_id)


async def _library_ready_job(msg_id: str, raw: dict[bytes, bytes]) -> None:
    stream = STREAM_LIBRARY_READY
    fields = await _decode_or_settle(stream, msg_id, raw)
    if fields is None:
        return
    work = await _run_cancelable(mark_library_ready(int(fields["library_id"])))
    if work.exception() is None:
        await _settle(stream, msg_id)
        return
    logger.error("mark library ready failed library=%s\n%s", fields.get("library_id", "?"), _tb(work.exception()))
    await _retry_or_fail(stream, msg_id, raw, lambda: _library_ready_giveup(fields.get("library_id", "?")))


async def normalize() -> None:
    cap = get_capacity("normalize", NORMALIZE_CONCURRENCY)
    profiles = make_profile_pool(cap.limit)
    await _drive(STREAM_INGEST, cap, lambda mid, raw: _spawn(_normalize_job(cap, profiles, mid, raw)))


async def paginate() -> None:
    cap = get_capacity("paginate", PAGINATE_CONCURRENCY)
    await _drive(STREAM_NORMALIZED, cap, lambda mid, raw: _spawn(_paginate_job(cap, mid, raw)))


async def _claim_ready(consumer: str, cap: GlobalCapacity, spawn: Spawn, room: int) -> int:
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
            await cap.take()
            spawn(msg_id.decode(), raw)
            claimed += 1
    return claimed


async def _recover_render(consumer: str, cap: GlobalCapacity, spawn: Spawn) -> None:
    redis = get_redis()
    for name in cast("set[bytes]", await redis.smembers(RENDER_DOCS)):
        stream = render_stream(name.decode())
        await ensure_group(stream)
        await _recover(stream, consumer, cap, spawn)


async def _render_loop(consumer: str, cap: GlobalCapacity, spawn: Spawn) -> None:
    while True:
        if await cap.free() <= 0:
            await cap.wait_free()
            continue
        room = await cap.free()
        if room <= 0 or not await _claim_ready(consumer, cap, spawn, room):
            await asyncio.sleep(0.1)


async def render() -> None:
    await ensure_group(STREAM_PAGES)
    cap = get_capacity("render", RENDER_CONCURRENCY)
    consumer = f"render-{HOSTNAME}"

    def spawn(mid, raw):
        return _spawn(_render_job(cap, mid, raw))

    await _recover_render(consumer, cap, spawn)
    await _render_loop(consumer, cap, spawn)


async def rasterize() -> None:
    cap = get_capacity("rasterize", RASTERIZE_PULL_CONCURRENCY)
    await _drive(STREAM_RASTERIZE, cap, lambda mid, raw: _spawn(_rasterize_job(cap, mid, raw)))


async def ocr() -> None:
    cap = get_capacity("ocr-stream", OCR_CONCURRENCY)
    await _drive(STREAM_PAGES, cap, lambda mid, raw: _spawn(_ocr_job(cap, mid, raw)))


async def merge() -> None:
    cap = get_capacity("merge", MERGE_CONCURRENCY)
    await _drive(STREAM_MERGE, cap, lambda mid, raw: _spawn(_merge_job(cap, mid, raw)))


async def structure() -> None:
    cap = get_capacity("structure", STRUCTURE_CONCURRENCY)
    await _drive(STREAM_STRUCTURE, cap, lambda mid, raw: _spawn(_structure_job(cap, mid, raw)))


async def table_structure() -> None:
    await _drive(STREAM_TABLE_STRUCTURE, None, lambda mid, raw: _spawn(_table_structure_job(mid, raw)))


async def batch() -> None:
    await _drive(STREAM_BATCH, None, lambda mid, raw: _spawn(_batch_job(mid, raw)))


async def embed() -> None:
    await _drive(STREAM_EMBED, None, lambda mid, raw: _spawn(_embed_job(mid, raw)))


async def library_ready() -> None:
    await _drive(STREAM_LIBRARY_READY, None, lambda mid, raw: _spawn(_library_ready_job(mid, raw)))


async def _main() -> None:
    await reap_orphan_blobs()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    stages = (
        normalize,
        paginate,
        render,
        rasterize,
        ocr,
        structure,
        table_structure,
        merge,
        batch,
        embed,
        library_ready,
    )
    consumers = [asyncio.create_task(stage()) for stage in stages]
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
