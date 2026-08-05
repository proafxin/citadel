import functools

import redis.asyncio as aioredis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

from config import get_settings

REDIS_MAX_CONNECTIONS = 64


@functools.lru_cache
def get_redis() -> aioredis.Redis:
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
