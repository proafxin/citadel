import asyncio
import json
import logging
import traceback

from redis.asyncio import Redis

from citadel.embedding import embed_texts, get_embedder
from citadel.services.ingestion import GROUP, STREAM_EMBED, ensure_group, get_redis
from citadel.services.retrieval import embed_document
from config import configure_logging

logger = logging.getLogger(__name__)

BLOCK_MS = 5000
CONSUMER = "embed-0"
REPLY_TTL = 60


async def _handle(redis: Redis, raw: dict[bytes, bytes]) -> None:
    if b"texts" in raw:
        vectors = await asyncio.to_thread(embed_texts, json.loads(raw[b"texts"]))
        reply = raw[b"reply"].decode()
        await redis.rpush(f"embed:reply:{reply}", json.dumps(vectors))
        await redis.expire(f"embed:reply:{reply}", REPLY_TTL)
        return
    doc_id = int(raw[b"doc_id"])
    await embed_document(doc_id)
    logger.info("embed doc=%s", doc_id)


async def _process(redis: Redis, msg_id: str, raw: dict[bytes, bytes]) -> None:
    work = asyncio.create_task(_handle(redis, raw))
    await asyncio.wait({work})
    exc = work.exception()
    if exc is not None:
        logger.error("embed failed\n%s", "".join(traceback.format_exception(exc)))
    await redis.xack(STREAM_EMBED, GROUP, msg_id)
    await redis.xdel(STREAM_EMBED, msg_id)


async def _main() -> None:
    await asyncio.to_thread(get_embedder)
    redis = get_redis()
    await ensure_group(STREAM_EMBED)
    logger.info("embedding worker ready concurrency=1")
    while True:
        fresh = await redis.xreadgroup(GROUP, CONSUMER, {STREAM_EMBED: ">"}, count=1, block=BLOCK_MS)
        for _stream, entries in fresh or []:
            for msg_id, raw in entries:
                await _process(redis, msg_id.decode(), raw)


def main() -> None:
    configure_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
