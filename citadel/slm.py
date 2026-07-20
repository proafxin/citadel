import asyncio
import functools
import json
import logging
import signal
import traceback
from collections.abc import Coroutine
from typing import Any, cast

import httpx
from redis.commands.core import AsyncScript
from redis.exceptions import ResponseError

from citadel.bus import get_redis
from citadel.services.slm import (
    CHUNK,
    DONE,
    FAILED,
    REPLY_MAXLEN,
    SLM_GROUP,
    STREAM_SLM_BULK,
    STREAM_SLM_INTERACTIVE,
)
from config import configure_logging, get_settings

logger = logging.getLogger(__name__)

CONSUMER = "slm"
PROVIDER_CONCURRENCY = 3
MAX_ATTEMPTS = 3
BLOCK_MS = 5000
NO_TIMEOUT = httpx.Timeout(None)

_tasks: set[asyncio.Task[None]] = set()
_slot_free = asyncio.Event()


async def _ensure_group(stream: str) -> None:
    try:
        await get_redis().xgroup_create(stream, SLM_GROUP, id="0", mkstream=True)
    except ResponseError:
        logger.debug("group exists stream=%s", stream)


async def _emit(reply_to: str, job_id: str, kind: str, value: str) -> None:
    redis = get_redis()
    await redis.xadd(reply_to, {"job_id": job_id, "kind": kind, "value": value})
    await redis.xtrim(reply_to, maxlen=REPLY_MAXLEN, approximate=True)


async def _stream_call(client: httpx.AsyncClient, url: str, payload: dict, reply_to: str, job_id: str) -> None:
    async with client.stream("POST", url, json=payload) as response:
        if response.is_error:
            body = await response.aread()
            logger.error("provider %d job=%s body=%s", response.status_code, job_id, body.decode()[:2000])
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: ") :]
            if data == "[DONE]":
                break
            delta = json.loads(data)["choices"][0]["delta"].get("content")
            if delta:
                await _emit(reply_to, job_id, CHUNK, delta)


async def _blocking_call(client: httpx.AsyncClient, url: str, payload: dict, reply_to: str, job_id: str) -> None:
    response = await client.post(url, json=payload)
    if response.is_error:
        logger.error("provider %d job=%s body=%s", response.status_code, job_id, response.text[:2000])
    response.raise_for_status()
    await _emit(reply_to, job_id, CHUNK, response.json()["choices"][0]["message"]["content"])


async def _call_provider(payload: dict, reply_to: str, job_id: str) -> None:
    url = f"{get_settings().qwen_base_url}/chat/completions"
    async with httpx.AsyncClient(timeout=NO_TIMEOUT) as client:
        if payload.get("stream"):
            await _stream_call(client, url, payload, reply_to, job_id)
        else:
            await _blocking_call(client, url, payload, reply_to, job_id)


async def _settle(stream: str, msg_id: str) -> None:
    redis = get_redis()
    await redis.xack(stream, SLM_GROUP, msg_id)
    await redis.xdel(stream, msg_id)


_REQUEUE_LUA = """
redis.call('XADD', KEYS[1], '*', unpack(ARGV, 3))
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
redis.call('XDEL', KEYS[1], ARGV[2])
"""


@functools.lru_cache
def _requeue_script() -> AsyncScript:
    return get_redis().register_script(_REQUEUE_LUA)


async def _requeue(stream: str, msg_id: str, raw: dict[bytes, bytes], attempt: int) -> None:
    fields = {**raw, b"attempt": str(attempt).encode()}
    flat = [item for pair in fields.items() for item in pair]
    await _requeue_script()(keys=[stream], args=[SLM_GROUP, msg_id, *flat])


async def _fail(stream: str, msg_id: str, raw: dict[bytes, bytes], attempt: int) -> None:
    job_id = raw[b"job_id"].decode()
    reply_to = raw[b"reply_to"].decode()
    if attempt + 1 >= MAX_ATTEMPTS:
        await _emit(reply_to, job_id, FAILED, "provider call failed")
        await _settle(stream, msg_id)
    else:
        await _requeue(stream, msg_id, raw, attempt + 1)


async def _run_job(semaphore: asyncio.Semaphore, stream: str, msg_id: str, raw: dict[bytes, bytes]) -> None:
    job_id = raw[b"job_id"].decode()
    reply_to = raw[b"reply_to"].decode()
    attempt = int(raw.get(b"attempt", b"0"))
    async with semaphore:
        try:
            await _call_provider(json.loads(raw[b"payload"].decode()), reply_to, job_id)
        except httpx.HTTPStatusError as error:
            logger.exception("slm job failed job=%s attempt=%d", job_id, attempt)
            if error.response.is_client_error:
                await _emit(reply_to, job_id, FAILED, f"provider rejected the request: {error.response.status_code}")
                await _settle(stream, msg_id)
            else:
                await _fail(stream, msg_id, raw, attempt)
            return
        except (httpx.HTTPError, json.JSONDecodeError, KeyError):
            logger.exception("slm job failed job=%s attempt=%d", job_id, attempt)
            await _fail(stream, msg_id, raw, attempt)
            return
    await _emit(reply_to, job_id, DONE, "")
    await _settle(stream, msg_id)


def _done(task: asyncio.Task[None]) -> None:
    _tasks.discard(task)
    _slot_free.set()
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("slm task crashed\n%s", "".join(traceback.format_exception(exc)))


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_done)


async def _drain(semaphore: asyncio.Semaphore, stream: str, count: int) -> int:
    fresh = cast(
        "list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]",
        await get_redis().xreadgroup(SLM_GROUP, CONSUMER, {stream: ">"}, count=count, block=BLOCK_MS),
    )
    entries = fresh[0][1] if fresh else []
    for msg_id, raw in entries:
        _spawn(_run_job(semaphore, stream, msg_id.decode(), raw))
    return len(entries)


async def _pump() -> None:
    semaphore = asyncio.Semaphore(PROVIDER_CONCURRENCY)
    while True:
        free = PROVIDER_CONCURRENCY - len(_tasks)
        if free <= 0:
            _slot_free.clear()
            await _slot_free.wait()
            continue
        if await _drain(semaphore, STREAM_SLM_INTERACTIVE, free):
            continue
        await _drain(semaphore, STREAM_SLM_BULK, free)


async def _main() -> None:
    configure_logging()
    for stream in (STREAM_SLM_INTERACTIVE, STREAM_SLM_BULK):
        await _ensure_group(stream)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("slm consumer up concurrency=%d", PROVIDER_CONCURRENCY)
    pump = asyncio.create_task(_pump())
    await stop.wait()
    pump.cancel()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
