import asyncio
from dataclasses import dataclass
from functools import lru_cache

from redis.commands.core import AsyncScript

from citadel.bus import get_redis

OCR_CONCURRENCY = 8
VISION_BUFFER = 0

TEXT_CONCURRENCY = 8
TEXT_BUFFER = 0

TEXT_LARGE_CONCURRENCY = 8
TEXT_LARGE_BUFFER = 0

INTERACTIVE_CONCURRENCY = 8
INTERACTIVE_BUFFER = 0

EMBED_CONCURRENCY = 384
EMBED_BUFFER = 128

POLL_INTERVAL_S = 0.2


_ACQUIRE_LUA = """
local n = tonumber(redis.call('GET', KEYS[1]) or '0')
if n >= tonumber(ARGV[1]) then return 0 end
redis.call('INCR', KEYS[1])
return 1
"""


@lru_cache
def _acquire_script() -> AsyncScript:
    return get_redis().register_script(_ACQUIRE_LUA)


@dataclass(frozen=True)
class GlobalCapacity:
    pool: str
    limit: int

    def _counter_key(self) -> str:
        return f"cap:{self.pool}"

    async def free(self) -> int:
        used = int(await get_redis().get(self._counter_key()) or 0)
        return self.limit - used

    async def _try_acquire(self) -> bool:
        script = _acquire_script()
        return bool(await script(keys=[self._counter_key()], args=[self.limit]))

    async def take(self, key: str = "") -> None:
        del key
        while not await self._try_acquire():  # ruff: ignore[async-busy-wait] -- polling cross-process Redis state, not a local condition
            await asyncio.sleep(POLL_INTERVAL_S)

    async def release(self, key: str = "") -> None:
        del key
        await get_redis().decr(self._counter_key())

    async def acquire(self, key: str = "") -> None:
        await self.take(key)

    async def wait_free(self) -> None:
        while await self.free() <= 0:  # ruff: ignore[async-busy-wait] -- polling cross-process Redis state, not a local condition
            await asyncio.sleep(POLL_INTERVAL_S)


@lru_cache
def get_capacity(pool: str, limit: int) -> GlobalCapacity:
    return GlobalCapacity(pool, limit)


@lru_cache
def get_vision_capacity() -> GlobalCapacity:
    return GlobalCapacity("vision", OCR_CONCURRENCY + VISION_BUFFER)


@lru_cache
def get_text_capacity() -> GlobalCapacity:
    return GlobalCapacity("text-structure", TEXT_CONCURRENCY + TEXT_BUFFER)


@lru_cache
def get_interactive_capacity() -> GlobalCapacity:
    return GlobalCapacity("interactive", INTERACTIVE_CONCURRENCY + INTERACTIVE_BUFFER)


@lru_cache
def get_text_large_capacity() -> GlobalCapacity:
    return GlobalCapacity("text-large", TEXT_LARGE_CONCURRENCY + TEXT_LARGE_BUFFER)


@lru_cache
def get_embed_capacity() -> GlobalCapacity:
    return GlobalCapacity("embed", EMBED_CONCURRENCY + EMBED_BUFFER)
