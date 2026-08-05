from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from config import get_settings

POOL_SIZE = 30
MAX_OVERFLOW = 20
IDLE_TX_TIMEOUT_MS = 60_000


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(
        get_settings().database_url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
        connect_args={"server_settings": {"idle_in_transaction_session_timeout": str(IDLE_TX_TIMEOUT_MS)}},
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)
