import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from citadel.router import router
from citadel.services.retrieval import embed_all_pending, embed_library
from config import configure_logging, get_embedder, get_settings

logger = logging.getLogger(__name__)


async def _drain_embeds(queue: asyncio.Queue[int]) -> None:
    while True:
        library_id = await queue.get()
        logger.info("embedding library=%d", library_id)
        embedded = await embed_library(library_id)
        logger.info("embedded library=%d nodes=%d", library_id, embedded)


async def _catchup() -> None:
    embedded = await embed_all_pending()
    if embedded:
        logger.info("startup catch-up embedded nodes=%d", embedded)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    await asyncio.to_thread(get_embedder)
    queue: asyncio.Queue[int] = asyncio.Queue()
    conn = await asyncpg.connect(get_settings().pg_dsn)
    await conn.add_listener("embed", lambda _conn, _pid, _channel, payload: queue.put_nowait(int(payload)))
    tasks = [asyncio.create_task(_drain_embeds(queue)), asyncio.create_task(_catchup())]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await conn.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
