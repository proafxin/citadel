import asyncio
import functools
import json
import logging
import signal
import time
import traceback
from base64 import b64encode
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
DRAIN_COUNT = 256
MAX_ATTEMPTS = 3
BLOCK_MS = 5000
NO_TIMEOUT = httpx.Timeout(None)

_tasks: set[asyncio.Task[None]] = set()


async def _ensure_group(stream: str) -> None:
    try:
        await get_redis().xgroup_create(stream, SLM_GROUP, id="0", mkstream=True)
    except ResponseError:
        logger.debug("group exists stream=%s", stream)


async def _emit(reply_to: str, job_id: str, kind: str, value: str) -> None:
    redis = get_redis()
    await redis.xadd(reply_to, {"job_id": job_id, "kind": kind, "value": value})
    await redis.xtrim(reply_to, maxlen=REPLY_MAXLEN, approximate=True)


async def _stream_call(client: httpx.AsyncClient, url: str, payload: dict, reply_to: str, job_id: str) -> float | None:
    first: float | None = None
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
                if first is None:
                    first = time.time()
                await _emit(reply_to, job_id, CHUNK, delta)
    return first


async def _blocking_call(
    client: httpx.AsyncClient, url: str, payload: dict, reply_to: str, job_id: str
) -> float | None:
    response = await client.post(url, json=payload)
    if response.is_error:
        logger.error("provider %d job=%s body=%s", response.status_code, job_id, response.text[:2000])
    response.raise_for_status()
    first = time.time()
    await _emit(reply_to, job_id, CHUNK, response.json()["choices"][0]["message"]["content"])
    return first


async def _resolve_images(payload: dict) -> dict:
    redis = get_redis()
    for message in payload.get("messages", []):
        for item in message.get("content", []):
            if item.get("type") != "image_ref":
                continue
            image_key = item.pop("image_key")
            data = await redis.get(image_key)
            if data is None:
                raise KeyError(image_key)
            item["type"] = "image_url"
            item["image_url"] = {"url": "data:image/png;base64," + b64encode(data).decode()}
    return payload


async def _call_provider(payload: dict, reply_to: str, job_id: str) -> float | None:
    payload = await _resolve_images(payload)
    url = f"{get_settings().qwen_base_url}/chat/completions"
    async with httpx.AsyncClient(timeout=NO_TIMEOUT) as client:
        if payload.get("stream"):
            return await _stream_call(client, url, payload, reply_to, job_id)
        return await _blocking_call(client, url, payload, reply_to, job_id)


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


async def _run_job(stream: str, msg_id: str, raw: dict[bytes, bytes]) -> None:
    job_id = raw[b"job_id"].decode()
    reply_to = raw[b"reply_to"].decode()
    attempt = int(raw.get(b"attempt", b"0"))
    emitted = float(raw.get(b"t_emit", b"0") or 0)
    started = time.time()
    try:
        first = await _call_provider(json.loads(raw[b"payload"].decode()), reply_to, job_id)
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
    done = time.time()
    ttft = (first - emitted) if first and emitted else (started - emitted if emitted else 0.0)
    gen = (done - first) if first else (done - started)
    level = logging.INFO if stream == STREAM_SLM_INTERACTIVE else logging.DEBUG
    logger.log(level, "slm job stream=%s ttft=%.1fs gen=%.1fs", stream, ttft, gen)
    await _emit(reply_to, job_id, DONE, "")
    await _settle(stream, msg_id)


def _done(task: asyncio.Task[None]) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("slm task crashed\n%s", "".join(traceback.format_exception(exc)))


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_done)


async def _drain(stream: str) -> int:
    fresh = cast(
        "list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]",
        await get_redis().xreadgroup(SLM_GROUP, CONSUMER, {stream: ">"}, count=DRAIN_COUNT, block=BLOCK_MS),
    )
    entries = fresh[0][1] if fresh else []
    for msg_id, raw in entries:
        _spawn(_run_job(stream, msg_id.decode(), raw))
    return len(entries)


async def _pump() -> None:
    while True:
        if await _drain(STREAM_SLM_INTERACTIVE):
            continue
        await _drain(STREAM_SLM_BULK)


async def _main() -> None:
    configure_logging()
    for stream in (STREAM_SLM_INTERACTIVE, STREAM_SLM_BULK):
        await _ensure_group(stream)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("slm consumer up — dispatch unbounded, vllm schedules")
    pump = asyncio.create_task(_pump())
    await stop.wait()
    pump.cancel()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
