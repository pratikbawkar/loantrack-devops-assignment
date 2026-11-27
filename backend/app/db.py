"""PostgreSQL connection pool, startup retry monitor and readiness check.

Design:
- The pool is created with open=False and opened ONCE with open(wait=False).
  It never blocks startup. psycopg_pool's own background workers create
  connections and re-create them after PostgreSQL comes back.
- We never call pool.open(wait=True) or pool.wait(): a wait() timeout closes the pool.
- A separate monitor task runs SELECT 1 through the pool. When it fails it logs
  the attempt number and the next delay, and backs off exponentially
  (0.5s, 1s, 2s ... capped at 30s). When it succeeds it logs once and then
  re-checks every HEALTHY_CHECK_INTERVAL_SECONDS.
- Only exception CLASS names are logged. Driver messages can contain host,
  port, user or database names, and the conninfo contains the password.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from .config import Settings

logger = logging.getLogger("loantrack.db")

POOL_TIMEOUT_SECONDS = 5.0  # max wait for a pooled connection in a request
READY_CHECK_TIMEOUT_SECONDS = 2.0  # budget for one SELECT 1 (readiness + monitor)
HEALTHY_CHECK_INTERVAL_SECONDS = 5.0  # monitor period while the database is reachable

# Errors that mean "the database cannot be reached right now" -> HTTP 503.
DB_UNAVAILABLE_ERRORS: tuple[type[BaseException], ...] = (
    psycopg.OperationalError,
    psycopg.InterfaceError,
    PoolTimeout,
    TimeoutError,  # asyncio.TimeoutError is an alias of TimeoutError on Python 3.11+
)


class DatabaseUnavailable(Exception):
    """The database could not be reached. str() is an exception class name only."""


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Delay before retry number `attempt` (1-based): base * 2^(attempt-1), capped."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    # Avoid huge exponents once we are far past the cap.
    exponent = min(attempt - 1, 32)
    return min(cap, base * (2**exponent))


def _default_pool_factory(settings: Settings) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        timeout=POOL_TIMEOUT_SECONDS,
        kwargs={"connect_timeout": settings.connect_timeout_seconds},
        # Test each connection before handing it out, so a connection broken
        # by a PostgreSQL restart is replaced instead of failing a request.
        check=AsyncConnectionPool.check_connection,
        open=False,
        name="loantrack",
    )


class Database:
    def __init__(
        self,
        settings: Settings,
        pool_factory: Callable[[Settings], Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._pool_factory = pool_factory or _default_pool_factory
        self._sleep = sleep
        self.pool: Any = None
        self._monitor_task: asyncio.Task[None] | None = None
        self.db_ok = False  # last result seen by the monitor (informational)

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Create and open the pool without waiting, then start the monitor. Never blocks."""
        self.pool = self._pool_factory(self._settings)
        await self.pool.open(wait=False)
        self._monitor_task = asyncio.create_task(self._monitor(), name="db-monitor")
        logger.info("database pool opened in background; connectivity monitor started")

    async def stop(self) -> None:
        """Cancel the monitor task, then close the pool."""
        task, self._monitor_task = self._monitor_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        pool, self.pool = self.pool, None
        if pool is not None:
            await pool.close()
        logger.info("database monitor stopped and pool closed")

    # ---- checks ----------------------------------------------------------

    async def check(self, timeout: float = READY_CHECK_TIMEOUT_SECONDS) -> tuple[bool, str | None]:
        """Run SELECT 1 through the pool. Returns (ok, error_class_name)."""
        if self.pool is None:
            return False, "PoolNotStarted"
        try:
            await asyncio.wait_for(self._select_one(timeout), timeout=timeout)
            return True, None
        except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
            return False, type(exc).__name__

    async def _select_one(self, timeout: float) -> None:
        async with self.pool.connection(timeout=timeout) as conn:
            await conn.execute("SELECT 1")

    async def _monitor(self) -> None:
        attempt = 0
        ever_connected = False
        while True:
            ok, error = await self.check()
            if ok:
                if not self.db_ok:
                    if ever_connected:
                        logger.info("database connectivity restored after %d failed attempt(s)", attempt)
                    else:
                        logger.info("database connectivity established after %d failed attempt(s)", attempt)
                ever_connected = True
                self.db_ok = True
                attempt = 0
                delay = HEALTHY_CHECK_INTERVAL_SECONDS
            else:
                self.db_ok = False
                attempt += 1
                delay = backoff_delay(attempt, self._settings.retry_base_seconds, self._settings.retry_max_seconds)
                logger.warning(
                    "database not reachable: attempt=%d retry_in=%.1fs error=%s",
                    attempt,
                    delay,
                    error,
                )
            await self._sleep(delay)

    # ---- request access --------------------------------------------------

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        """Borrow a pooled connection; translate connectivity errors into DatabaseUnavailable."""
        if self.pool is None:
            raise DatabaseUnavailable("PoolNotStarted")
        try:
            async with self.pool.connection(timeout=POOL_TIMEOUT_SECONDS) as conn:
                yield conn
        except DB_UNAVAILABLE_ERRORS as exc:
            raise DatabaseUnavailable(type(exc).__name__) from exc
