from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from config import get_settings

# sized to cover every stage that can hold a DB session at once, per process, with burst headroom: the worker's
# normalize+merge+tabular caps plus the app's concurrent queries/finalize. app + worker + listener must stay under the
# server's max_connections (raised to 200 in compose) — 50 per process leaves room for the listener and migrations.
POOL_SIZE = 30
MAX_OVERFLOW = 20
# a hard-killed app (SIGKILL / crash / dev-reload) can't run graceful cleanup, so it can leave a backend with an open
# transaction holding row/index locks that blocks the next run's writes indefinitely. bound it two ways: the server
# terminates any of OUR sessions left idle-in-transaction past this, and TCP keepalives (compose) reap a mid-statement
# orphan whose client vanished. no legit transaction here sits idle this long (none awaits non-DB work mid-txn).
IDLE_TX_TIMEOUT_MS = 60_000


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(
        get_settings().database_url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,  # discard a connection the server already dropped instead of erroring on first use
        connect_args={"server_settings": {"idle_in_transaction_session_timeout": str(IDLE_TX_TIMEOUT_MS)}},
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)
