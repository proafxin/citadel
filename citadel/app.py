import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from citadel.router import router
from citadel.services.ingestion import STREAMS, ensure_group
from citadel.services.retrieval import embed_pending
from config import configure_logging, get_embedder

EMBED_POLL_SECONDS = 5


async def _embed_loop() -> None:
    while True:
        embedded = await embed_pending()
        if not embedded:
            await asyncio.sleep(EMBED_POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    for stream in STREAMS:
        await ensure_group(stream)
    await asyncio.to_thread(get_embedder)
    task = asyncio.create_task(_embed_loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(lifespan=lifespan)
app.include_router(router)
