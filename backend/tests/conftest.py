"""Shared test fixtures.

Unit tests never need a database: they use FakePool, whose availability can be
switched at runtime to simulate PostgreSQL going down and coming back.

Integration tests (test_api_integration.py) need TEST_DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

# app.main builds a module-level `app` at import time, which reads configuration.
# Give it dummy, unreachable values so importing it in tests is always safe.
for _name, _value in {
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "1",
    "POSTGRES_DB": "dummy_db",
    "POSTGRES_USER": "dummy_user",
    "POSTGRES_PASSWORD": "dummy_password",
}.items():
    os.environ.setdefault(_name, _value)

from app.config import Settings, load_settings  # noqa: E402
from app.main import create_app  # noqa: E402

# Fake value, not a real credential: tests plant it in config and errors, then
# assert it never appears in logs, responses or tracebacks.
SECRET_PASSWORD = "S3cr3t-Pa55w0rd-NEVER-LOG"

BASE_ENV = {
    "DB_HOST": "db.internal.example",
    "DB_PORT": "5432",
    "POSTGRES_DB": "loantrack",
    "POSTGRES_USER": "loantrack_user",
    "POSTGRES_PASSWORD": SECRET_PASSWORD,
    "DB_RETRY_BASE_SECONDS": "0.5",
    "DB_RETRY_MAX_SECONDS": "30",
    "APP_VERSION": "v1",
    "HOSTNAME": "pod-abc",
}


def seed_rows() -> list[dict[str, Any]]:
    base = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)
    data = [
        ("Alice Morgan", "250000.00", "Austin", "APPROVED"),
        ("Ben Carter", "180500.50", "Denver", "PENDING"),
        ("Chloe Nguyen", "425000.00", "Seattle", "UNDER_REVIEW"),
        ("David Okafor", "95000.75", "Phoenix", "PENDING"),
        ("Emma Lindqvist", "312000.00", None, "APPROVED"),
    ]
    return [
        {
            "id": i,
            "borrower_name": name,
            "loan_amount": Decimal(amount),
            "property_city": city,
            "status": status,
            "created_at": base + timedelta(minutes=i),
        }
        for i, (name, amount, city, status) in enumerate(data, start=1)
    ]


class FakeCursor:
    def __init__(self, pool: "FakePool") -> None:
        self._pool = pool
        self._result: list[Any] = []

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> "FakeCursor":
        self._pool.executed.append((sql, params))
        if self._pool.fail_queries:
            # Message deliberately contains secrets: it must never reach the logs.
            raise psycopg.OperationalError(
                f'connection to server at "db.internal.example" failed: password={SECRET_PASSWORD}'
            )
        if sql.lstrip().upper().startswith("INSERT"):
            assert "'PENDING'" in sql, "status must be set by the server"
            row = {
                "id": max((r["id"] for r in self._pool.rows), default=0) + 1,
                "borrower_name": params["borrower_name"],
                "loan_amount": Decimal(params["loan_amount"]).quantize(Decimal("0.01")),
                "property_city": params["property_city"],
                "status": "PENDING",
                "created_at": datetime.now(timezone.utc),
            }
            self._pool.rows.append(row)
            self._result = [row]
        elif "FROM loans" in sql:
            self._result = sorted(self._pool.rows, key=lambda r: r["id"], reverse=True)
        else:  # SELECT 1
            self._result = [(1,)]
        return self

    async def fetchall(self) -> list[Any]:
        return list(self._result)

    async def fetchone(self) -> Any:
        return self._result[0] if self._result else None


class FakeConnection:
    def __init__(self, pool: "FakePool") -> None:
        self._pool = pool

    def cursor(self, row_factory: Any = None) -> FakeCursor:
        return FakeCursor(self._pool)

    async def execute(self, sql: str, params: Any = None) -> FakeCursor:
        return await FakeCursor(self._pool).execute(sql, params)


class FakePool:
    """Stands in for psycopg_pool.AsyncConnectionPool.

    available=False  -> connection() raises PoolTimeout (database down)
    fail_queries=True -> queries raise OperationalError (connection dropped mid-request)
    hang=True        -> connection() never returns (database stuck)
    """

    def __init__(self) -> None:
        self.available = True
        self.fail_queries = False
        self.hang = False
        self.rows = seed_rows()
        self.executed: list[tuple[str, Any]] = []
        self.open_calls: list[bool] = []
        self.wait_calls = 0
        self.close_calls = 0
        self.connection_calls = 0

    async def open(self, wait: bool = True, timeout: float = 30.0) -> None:
        self.open_calls.append(wait)

    async def wait(self, timeout: float = 30.0) -> None:  # must never be called
        self.wait_calls += 1

    async def close(self, timeout: float = 5.0) -> None:
        self.close_calls += 1

    @asynccontextmanager
    async def connection(self, timeout: float | None = None):
        self.connection_calls += 1
        if self.hang:
            await asyncio.Event().wait()
        if not self.available:
            raise PoolTimeout(f"couldn't get a connection; password={SECRET_PASSWORD}")
        yield FakeConnection(self)

    def inserts(self) -> list[tuple[str, Any]]:
        return [(s, p) for s, p in self.executed if s.lstrip().upper().startswith("INSERT")]


@pytest.fixture
def fake_pool() -> FakePool:
    return FakePool()


def make_settings(**overrides: str) -> Settings:
    env = {**BASE_ENV, **overrides}
    return load_settings({k: v for k, v in env.items() if v is not None})


@pytest.fixture
def make_client(fake_pool: FakePool) -> Callable[..., TestClient]:
    """Build a TestClient around a fresh app using fake_pool. Env overrides as kwargs."""
    clients: list[TestClient] = []

    def _make(**env: str) -> TestClient:
        app = create_app(make_settings(**env), pool_factory=lambda settings: fake_pool)
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()  # runs the lifespan (pool open + monitor start)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)  # runs shutdown (monitor cancel + pool close)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()
