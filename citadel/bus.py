import functools

import redis.asyncio as aioredis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

from config import get_settings

REDIS_MAX_CONNECTIONS = 64  # bounded blocking pool: callers queue for a connection, never open unbounded sockets


@functools.lru_cache
def get_redis() -> aioredis.Redis:
    # the one redis handle, defined HERE rather than in the ingestion service because it is not an ingestion concern:
    # it is the shared bus, and the SLM's global slot pool needs it too. ingestion importing llm (via the tabular
    # structure call) means llm cannot import back from ingestion, so the client lives below both.
    #
    # bounded blocking pool: a burst queues for a free connection instead of opening unbounded sockets (which starve
    # getaddrinfo on the shared executor → connect timeouts). socket_timeout=None so blocking XREADGROUP/BLPOP isn't
    # cut off; keepalive + health check drop dead connections; retry reconnects a blip instead of crashing the pump.
    pool = aioredis.BlockingConnectionPool.from_url(
        get_settings().redis_url,
        max_connections=REDIS_MAX_CONNECTIONS,
        timeout=None,
        socket_timeout=None,
        socket_connect_timeout=5,
        socket_keepalive=True,
        health_check_interval=30,
    )
    return aioredis.Redis(
        connection_pool=pool,
        retry=Retry(ExponentialBackoff(cap=1.0, base=0.1), 3),
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
    )
