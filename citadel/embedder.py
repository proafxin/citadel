import asyncio
import logging
import traceback

from redis.asyncio import Redis

from citadel.embedding import get_embedder
from citadel.services.ingestion import GROUP, STREAM_EMBED, ensure_group, get_redis
from citadel.services.retrieval import embed_document
from config import configure_logging

logger = logging.getLogger(__name__)

BLOCK_MS = 5000
CONSUMER = "embed-0"


async def _process(redis: Redis, msg_id: str, doc_id: str) -> None:
    work = asyncio.create_task(embed_document(int(doc_id)))
    await asyncio.wait({work})
    exc = work.exception()
    if exc is not None:
        logger.error("embed failed doc=%s\n%s", doc_id, "".join(traceback.format_exception(exc)))
    else:
        logger.info("embed doc=%s", doc_id)
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
                await _process(redis, msg_id.decode(), raw[b"doc_id"].decode())


def main() -> None:
    configure_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
