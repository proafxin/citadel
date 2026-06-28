from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from citadel.router import router
from citadel.services.ingestion import STREAMS, ensure_group
from config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    for stream in STREAMS:
        await ensure_group(stream)
    # retrieval disabled at startup for now — re-enable once the HF cache perms are fixed:
    #   import asyncio; from citadel.services import retrieval
    #   await asyncio.to_thread(retrieval.get_embedder)
    #   await asyncio.to_thread(retrieval.get_reranker)
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(router)
