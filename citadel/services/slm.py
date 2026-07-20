import asyncio
import functools
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import cast

from citadel.bus import get_redis

logger = logging.getLogger(__name__)

STREAM_SLM_INTERACTIVE = "slm_interactive"
STREAM_SLM_BULK = "slm_bulk"
SLM_GROUP = "citadel"

CHUNK = "chunk"
DONE = "done"
FAILED = "failed"

REPLY_MAXLEN = 10_000


@functools.lru_cache
def reply_stream() -> str:
    return f"slm_reply_{uuid.uuid4().hex}"


@functools.lru_cache
def _pending() -> dict[str, asyncio.Queue[tuple[str, str]]]:
    return {}


async def submit(payload: dict, interactive: bool) -> AsyncIterator[str]:
    job_id = uuid.uuid4().hex
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    _pending()[job_id] = queue
    stream = STREAM_SLM_INTERACTIVE if interactive else STREAM_SLM_BULK
    try:
        await get_redis().xadd(
            stream,
            {"job_id": job_id, "reply_to": reply_stream(), "payload": json.dumps(payload)},
        )
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


async def collect(payload: dict, interactive: bool) -> str:
    return "".join([chunk async for chunk in submit(payload, interactive)])


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
