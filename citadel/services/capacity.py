import asyncio
from dataclasses import dataclass, field
from functools import lru_cache

OCR_CONCURRENCY = 32
VISION_BUFFER = 16
TEXT_CONCURRENCY = 64
TEXT_BUFFER = 32


@dataclass
class Capacity:
    limit: int
    inflight: int = 0
    slot: asyncio.Event = field(default_factory=asyncio.Event)

    def free(self) -> int:
        return self.limit - self.inflight

    def take(self) -> None:
        self.inflight += 1

    def release(self) -> None:
        self.inflight -= 1
        self.slot.set()

    async def wait_free(self) -> None:
        self.slot.clear()
        if self.free() > 0:
            return
        await self.slot.wait()

    async def acquire(self) -> None:
        while self.free() <= 0:
            await self.wait_free()
        self.take()


@lru_cache
def get_vision_capacity() -> Capacity:
    return Capacity(OCR_CONCURRENCY + VISION_BUFFER)


@lru_cache
def get_text_capacity() -> Capacity:
    return Capacity(TEXT_CONCURRENCY + TEXT_BUFFER)
