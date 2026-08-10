import asyncio
import time
from dataclasses import dataclass, field
from functools import lru_cache

from redis.commands.core import AsyncScript

from citadel.bus import get_redis

OCR_CONCURRENCY = 32
VISION_BUFFER = 16
VISION_STALE_S = 120

TEXT_CONCURRENCY = 64
TEXT_BUFFER = 32
TEXT_STALE_S = 60

TEXT_LARGE_CONCURRENCY = 4
TEXT_LARGE_BUFFER = 2
TEXT_LARGE_STALE_S = 600

EMBED_CONCURRENCY = 16
EMBED_BUFFER = 8
EMBED_STALE_S = 60

WAIT_FALLBACK_S = 5.0


@dataclass
class Capacity:
    limit: int
    inflight: int = 0
    slot: asyncio.Event = field(default_factory=asyncio.Event)

    async def free(self) -> int:
        return self.limit - self.inflight

    async def take(self, key: str = "") -> None:
        del key
        self.inflight += 1

    async def release(self, key: str = "") -> None:
        del key
        self.inflight -= 1
        self.slot.set()

    async def wait_free(self) -> None:
        self.slot.clear()
        if await self.free() > 0:
            return
        await self.slot.wait()

    async def acquire(self, key: str = "") -> None:
        while await self.free() <= 0:
            await self.wait_free()
        await self.take(key)


_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1] - ARGV[2])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""


@lru_cache
def _acquire_script() -> AsyncScript:
    return get_redis().register_script(_ACQUIRE_LUA)


@dataclass(frozen=True)
class GlobalCapacity:
    pool: str
    limit: int
    stale_ttl_s: int

    def _zkey(self) -> str:
        return f"cap:{self.pool}"

    def _channel(self) -> str:
        return f"cap:{self.pool}:release"

    async def free(self) -> int:
        used = await get_redis().zcard(self._zkey())
        return self.limit - used

    async def _try_acquire(self, key: str) -> bool:
        script = _acquire_script()
        return bool(await script(keys=[self._zkey()], args=[time.time(), self.stale_ttl_s, self.limit, key]))

    async def take(self, key: str) -> None:
        if await self._try_acquire(key):
            return
        async with get_redis().pubsub(ignore_subscribe_messages=True) as pubsub:
            await pubsub.subscribe(self._channel())
            while not await self._try_acquire(key):
                await pubsub.get_message(timeout=WAIT_FALLBACK_S)

    async def release(self, key: str) -> None:
        redis = get_redis()
        await redis.zrem(self._zkey(), key)
        await redis.publish(self._channel(), key)

    async def acquire(self, key: str) -> None:
        await self.take(key)


@lru_cache
def get_vision_capacity() -> GlobalCapacity:
    return GlobalCapacity("vision", OCR_CONCURRENCY + VISION_BUFFER, VISION_STALE_S)


@lru_cache
def get_text_capacity() -> GlobalCapacity:
    return GlobalCapacity("text-structure", TEXT_CONCURRENCY + TEXT_BUFFER, TEXT_STALE_S)


@lru_cache
def get_text_large_capacity() -> GlobalCapacity:
    return GlobalCapacity("text-large", TEXT_LARGE_CONCURRENCY + TEXT_LARGE_BUFFER, TEXT_LARGE_STALE_S)


@lru_cache
def get_embed_capacity() -> GlobalCapacity:
    return GlobalCapacity("embed", EMBED_CONCURRENCY + EMBED_BUFFER, EMBED_STALE_S)
