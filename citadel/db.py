from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from config import get_settings

# sized to cover every stage that can hold a DB session at once, per process, with burst headroom: the worker's
# normalize+merge+tabular caps plus the app's concurrent queries/finalize. app + worker + listener must stay under the
# server's max_connections (raised to 200 in compose) — 50 per process leaves room for the listener and migrations.
POOL_SIZE = 30
MAX_OVERFLOW = 20


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_size=POOL_SIZE, max_overflow=MAX_OVERFLOW)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)
