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
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(router)
