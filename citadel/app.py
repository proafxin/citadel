import asyncio
import logging
import signal
import socket
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from citadel.bus import get_redis
from citadel.db import get_engine, get_sessionmaker
from citadel.models.library import Library
from citadel.models.status import LibraryStatus
from citadel.router import router
from citadel.services.batching import emit_library_batches
from citadel.services.document import STREAM_FINALIZE, mark_described, mark_embed_started
from citadel.services.ingestion import GROUP, ensure_group
from citadel.services.ingestion import shutdown as shutdown_resources
from citadel.services.readiness import flags_key
from citadel.services.retrieval import STREAM_EMBED
from config import configure_logging

logger = logging.getLogger(__name__)

HOSTNAME_CONSUMER = f"finalize-{socket.gethostname()}"
_FINALIZE_LOCK_CLASS = 3
FINALIZE_MIN_IDLE_MS = 60_000
FINALIZE_SWEEP_S = 30
FINALIZE_READ_COUNT = 16
FINALIZE_BLOCK_MS = 5_000


async def _claim_finalize(library_id: int) -> bool:
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:cls, :lib)"), {"cls": _FINALIZE_LOCK_CLASS, "lib": library_id}
        )
        library = await session.get(Library, library_id)
        if library is None or library.status != LibraryStatus.INGESTED or library.finalize_started_at is not None:
            return False
        library.finalize_started_at = datetime.now(UTC)
        return True


async def _finalize(library_id: int) -> None:
    if not await _claim_finalize(library_id):
        logger.info("finalize skipped (already claimed) library=%d", library_id)
        return
    logger.info("finalizing library=%d", library_id)
    await get_redis().delete(flags_key(library_id))
    await mark_described(library_id)
    emitted = await emit_library_batches(library_id)
    await mark_embed_started(library_id)
    await get_redis().xadd(STREAM_EMBED, {"library_id": str(library_id)})
    logger.info("finalized library=%d documents=%d — awaiting summaries+embed", library_id, emitted)


async def _settle_finalize(msg_id: bytes) -> None:
    redis = get_redis()
    await redis.xack(STREAM_FINALIZE, GROUP, msg_id)
    await redis.xdel(STREAM_FINALIZE, msg_id)


async def _consume_entries(entries: list[tuple[bytes, dict[bytes, bytes]]]) -> None:
    for msg_id, raw in entries:
        await _finalize(int(raw[b"library_id"]))
        await _settle_finalize(msg_id)


async def _drain_own(consumer: str) -> None:
    redis = get_redis()
    last = "0"
    while True:
        fresh = await redis.xreadgroup(GROUP, consumer, {STREAM_FINALIZE: last}, count=FINALIZE_READ_COUNT)
        entries = fresh[0][1] if fresh else []
        if not entries:
            return
        await _consume_entries(entries)
        last = entries[-1][0].decode()


async def _sweep_stale(consumer: str) -> None:
    redis = get_redis()
    while True:
        await asyncio.sleep(FINALIZE_SWEEP_S)
        _, claimed, _ = await redis.xautoclaim(
            STREAM_FINALIZE, GROUP, consumer, FINALIZE_MIN_IDLE_MS, "0-0", count=FINALIZE_READ_COUNT
        )
        if claimed:
            await _consume_entries(claimed)


async def consume_finalize() -> None:
    consumer = HOSTNAME_CONSUMER
    await ensure_group(STREAM_FINALIZE)
    await _drain_own(consumer)
    redis = get_redis()
    sweeper = asyncio.create_task(_sweep_stale(consumer))
    try:
        while True:
            fresh = await redis.xreadgroup(
                GROUP, consumer, {STREAM_FINALIZE: ">"}, count=FINALIZE_READ_COUNT, block=FINALIZE_BLOCK_MS
            )
            entries = fresh[0][1] if fresh else []
            if entries:
                await _consume_entries(entries)
    finally:
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)


def _fatal_on_worker_death(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.critical("finalization worker died — exiting\n%s", "".join(traceback.format_exception(exc)))
    else:
        logger.critical("finalization worker returned unexpectedly — exiting")
    signal.raise_signal(signal.SIGTERM)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    task = asyncio.create_task(consume_finalize())
    task.add_done_callback(_fatal_on_worker_death)
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await shutdown_resources()
    await get_engine().dispose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
