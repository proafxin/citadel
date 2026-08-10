import asyncio
import logging
import signal
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from citadel.bus import get_redis
from citadel.db import get_engine
from citadel.router import router
from citadel.services.batching import emit_library_batches
from citadel.services.document import mark_described, mark_embed_started, mark_finalize_started
from citadel.services.ingestion import shutdown as shutdown_resources
from citadel.services.readiness import flags_key
from citadel.services.retrieval import STREAM_EMBED, pending_libraries
from citadel.services.slm import read_replies
from config import configure_logging, get_settings

logger = logging.getLogger(__name__)


async def _finalize(library_id: int, tag: str) -> None:
    logger.info("finalizing library=%d", library_id)
    await get_redis().delete(flags_key(library_id))
    await mark_finalize_started(library_id)
    await mark_described(library_id)
    emitted = await emit_library_batches(library_id)
    await mark_embed_started(library_id)
    await get_redis().xadd(STREAM_EMBED, {"library_id": str(library_id)})
    logger.info("%s library=%d documents=%d — awaiting summaries+embed", tag, library_id, emitted)


async def finalize_libraries(queue: asyncio.Queue[int]) -> None:
    while True:
        library_id = await queue.get()
        await _finalize(library_id, "finalized")


LISTEN_HEALTH_S = 30
LISTEN_RETRY_S = 2
_LISTEN_ERRORS = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError)


async def _listen(queue: asyncio.Queue[int]) -> None:
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
    queue: asyncio.Queue[int] = asyncio.Queue()
    tasks = [
        asyncio.create_task(finalize_libraries(queue)),
        asyncio.create_task(_listen(queue)),
        asyncio.create_task(read_replies()),
    ]
    for task in tasks:
        task.add_done_callback(_fatal_on_worker_death)
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
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
