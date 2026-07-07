import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from citadel.db import get_engine
from citadel.router import router
from citadel.services.document import (
    describe_library_tables,
    mark_described,
    mark_embed_started,
    mark_finalize_started,
    mark_library_ready,
)
from citadel.services.ingestion import shutdown as shutdown_resources
from citadel.services.retrieval import embed_library, pending_libraries
from config import configure_logging, get_embedder, get_settings

logger = logging.getLogger(__name__)

_finalizing: set[int] = set()


async def _finalize(library_id: int, tag: str) -> None:
    if library_id in _finalizing:  # catch-up and the live NOTIFY can target the same library — run it once
        return
    _finalizing.add(library_id)
    logger.info("finalizing library=%d", library_id)
    try:
        await mark_finalize_started(library_id)
        t = time.time()
        described = await describe_library_tables(library_id)
        describe_s = time.time() - t
        await mark_described(library_id)
        await mark_embed_started(library_id)
        t = time.time()
        embedded = await embed_library(library_id)
        embed_s = time.time() - t
        await mark_library_ready(library_id)
        logger.info(
            "%s library=%d tables=%d describe=%.1fs nodes=%d embed=%.1fs",
            tag,
            library_id,
            described,
            describe_s,
            embedded,
            embed_s,
        )
    finally:
        _finalizing.discard(library_id)


async def _drain_embeds(queue: asyncio.Queue[int]) -> None:
    while True:
        library_id = await queue.get()
        await _finalize(library_id, "finalized")


LISTEN_HEALTH_S = 30  # probe the LISTEN connection this often so a silently dead/partitioned socket is detected
LISTEN_RETRY_S = 2  # backoff between reconnect attempts while the DB is unreachable
_LISTEN_ERRORS = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError)


async def _listen(queue: asyncio.Queue[int]) -> None:
    # own the embed LISTEN connection: reconnect on drop and re-sweep pending_libraries on every (re)connect, so a PG
    # restart/blip can't silently strand finalization. the periodic SELECT surfaces a dead socket the driver hasn't
    # noticed yet; catch-up recovers any NOTIFY missed during the gap.
    while True:
        try:
            conn = await asyncpg.connect(get_settings().pg_dsn)
        except _LISTEN_ERRORS:
            logger.warning("embed listener cannot connect — retrying")
            await asyncio.sleep(LISTEN_RETRY_S)
            continue
        try:
            await conn.add_listener("embed", lambda _conn, _pid, _channel, payload: queue.put_nowait(int(payload)))
            for library_id in await pending_libraries():
                queue.put_nowait(library_id)
            while True:
                await asyncio.sleep(LISTEN_HEALTH_S)
                await conn.execute("SELECT 1")
        except _LISTEN_ERRORS:
            logger.warning("embed listener connection lost — reconnecting")
        finally:
            conn.terminate()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    await asyncio.to_thread(get_embedder)
    queue: asyncio.Queue[int] = asyncio.Queue()
    tasks = [asyncio.create_task(_drain_embeds(queue)), asyncio.create_task(_listen(queue))]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
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
