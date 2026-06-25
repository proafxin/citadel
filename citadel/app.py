import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from citadel.router import router
from citadel.services import ingestion, retrieval
from config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    for stream in ingestion.STREAMS:
        await ingestion.ensure_group(stream)
    await asyncio.to_thread(retrieval.get_embedder)  # load BGE-M3 + reranker onto the GPU at startup
    await asyncio.to_thread(retrieval.get_reranker)
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(router)
