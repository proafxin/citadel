import asyncio
import functools
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import cast

from citadel.bus import get_redis
from config import get_settings

logger = logging.getLogger(__name__)

STREAM_SLM_INTERACTIVE = "slm_interactive"
STREAM_SLM_OCR = "slm_ocr"
STREAM_SLM_TEXT_TABLE = "slm_text_table"
STREAM_SLM_LARGE = "slm_large"
STREAM_SLM_EMBED = "slm_embed"
SLM_GROUP = "citadel"

CHUNK = "chunk"
DONE = "done"
FAILED = "failed"

REPLY_MAXLEN = 10_000


@functools.lru_cache
def reply_stream() -> str:
    return f"slm_reply_{get_settings().worker_id}"


@functools.lru_cache
def _pending() -> dict[str, asyncio.Queue[tuple[str, str]]]:
    return {}


async def emit(payload: dict, stream: str, key: str) -> str:
    _pending()[key] = asyncio.Queue()
    await get_redis().xadd(
        stream,
        {
            "job_id": key,
            "reply_to": reply_stream(),
            "payload": json.dumps(payload),
            "t_emit": str(time.time()),
        },
    )
    return key


async def stream_reply(job_id: str) -> AsyncIterator[str]:
    queue = _pending()[job_id]
    try:
        while True:
            kind, value = await queue.get()
            if kind == DONE:
                return
            if kind == FAILED:
                message = f"slm job failed: {value}"
                raise RuntimeError(message)
            yield value
    finally:
        _pending().pop(job_id, None)


async def collect_reply(job_id: str) -> str:
    return "".join([chunk async for chunk in stream_reply(job_id)])


async def submit(payload: dict, stream: str, key: str) -> AsyncIterator[str]:
    job_id = await emit(payload, stream, key)
    async for chunk in stream_reply(job_id):
        yield chunk


async def collect(payload: dict, stream: str, key: str) -> str:
    return "".join([chunk async for chunk in submit(payload, stream, key)])


async def read_replies() -> None:
    redis = get_redis()
    stream = reply_stream()
    last = "0"
    while True:
        entries = cast(
            "list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]",
            await redis.xread({stream: last}, block=0),
        )
        for _, messages in entries:
            for msg_id, raw in messages:
                last = msg_id.decode()
                queue = _pending().get(raw[b"job_id"].decode())
                if queue is not None:
                    queue.put_nowait((raw[b"kind"].decode(), raw[b"value"].decode()))
            await redis.xtrim(stream, maxlen=REPLY_MAXLEN, approximate=True)
