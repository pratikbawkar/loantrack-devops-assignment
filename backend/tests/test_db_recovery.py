"""Startup without a database, retry/backoff logging, recovery without restart, shutdown."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from app.config import load_settings
from app.db import HEALTHY_CHECK_INTERVAL_SECONDS, Database
from app.main import create_app

from conftest import SECRET_PASSWORD, FakePool, make_settings


# ---------------------------------------------------------------------------
# Database unavailable during startup (fake pool, through HTTP)
# ---------------------------------------------------------------------------


def test_startup_with_database_down_does_not_block(fake_pool, make_client):
    fake_pool.available = False
    started = time.monotonic()
    client = make_client()
    assert time.monotonic() - started < 2.0, "startup must not wait for the database"
    assert client.get("/healthz").status_code == 200


def test_app_stays_alive_while_database_down(fake_pool, make_client):
    fake_pool.available = False
    client = make_client()
    for _ in range(5):
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503


def test_health_is_200_when_database_down(fake_pool, make_client):
    fake_pool.available = False
    response = make_client().get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_does_not_touch_database_even_if_it_hangs(fake_pool, make_client):
    fake_pool.hang = True  # any pool access would block forever
    client = make_client()
    started = time.monotonic()
    assert client.get("/healthz").status_code == 200
    assert time.monotonic() - started < 1.0


def test_readiness_is_503_when_database_down(fake_pool, make_client):
    fake_pool.available = False
    response = make_client().get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "unavailable",
        "served_by": "pod-abc",
        "version": "v1",
    }


def test_loan_endpoints_are_503_when_database_down(fake_pool, make_client):
    fake_pool.available = False
    client = make_client()
    get = client.get("/loans")
    post = client.post("/loans", json={"borrower_name": "Jane", "loan_amount": 100})
    assert (get.status_code, post.status_code) == (503, 503)
    assert get.json() == post.json() == {"detail": "database unavailable"}
    assert fake_pool.inserts() == []


def test_loan_endpoints_are_503_when_connection_drops_mid_query(fake_pool, client):
    fake_pool.fail_queries = True
    assert client.get("/loans").status_code == 503
    assert client.post("/loans", json={"borrower_name": "Jane", "loan_amount": 100}).status_code == 503


# ---------------------------------------------------------------------------
# Recovery without restarting the backend
# ---------------------------------------------------------------------------


def test_recovers_when_database_comes_back_without_restart(fake_pool, make_client):
    fake_pool.available = False
    client = make_client()
    assert client.get("/readyz").status_code == 503
    assert client.get("/loans").status_code == 503

    fake_pool.available = True  # PostgreSQL is back; same app instance, no restart

    assert client.get("/readyz").status_code == 200
    loans = client.get("/loans")
    assert loans.status_code == 200 and len(loans.json()) == 5
    created = client.post("/loans", json={"borrower_name": "Back Online", "loan_amount": "10.00"})
    assert created.status_code == 201

    # The pool was opened exactly once and never re-opened or waited on.
    assert fake_pool.open_calls == [False]
    assert fake_pool.wait_calls == 0


def test_goes_unready_again_when_database_lost_after_running(fake_pool, client):
    assert client.get("/readyz").status_code == 200
    fake_pool.available = False
    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200
    fake_pool.available = True
    assert client.get("/readyz").status_code == 200


# ---------------------------------------------------------------------------
# Pool lifecycle: open(wait=False) once, never wait(), clean shutdown
# ---------------------------------------------------------------------------


async def test_pool_opened_once_with_wait_false_and_never_waited():
    pool = FakePool()
    pool.available = False
    db = Database(make_settings(DB_RETRY_BASE_SECONDS="0.01", DB_RETRY_MAX_SECONDS="0.02"), lambda s: pool)
    await db.start()
    await asyncio.sleep(0.2)  # several monitor retries happen here
    await db.stop()
    assert pool.open_calls == [False]
    assert pool.wait_calls == 0
    assert pool.connection_calls >= 3, "monitor should have retried several times"


async def test_shutdown_cancels_monitor_and_closes_pool():
    pool = FakePool()
    db = Database(make_settings(), lambda s: pool)
    await db.start()
    task = db._monitor_task
    assert task is not None and not task.done()
    await db.stop()
    assert task.cancelled()
    assert pool.close_calls == 1
    assert db.pool is None and db._monitor_task is None


def test_lifespan_shutdown_closes_pool(fake_pool):
    app = create_app(make_settings(), pool_factory=lambda s: fake_pool)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert fake_pool.close_calls == 0
    assert fake_pool.close_calls == 1


# ---------------------------------------------------------------------------
# Monitor: exponential backoff, logging, reset after success
# ---------------------------------------------------------------------------


async def test_monitor_backs_off_logs_attempts_and_resets(caplog):
    caplog.set_level(logging.INFO, logger="loantrack")
    pool = FakePool()
    pool.available = False
    delays: list[float] = []
    done = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        n = len(delays)
        if n == 8:
            pool.available = True  # database comes up after 8 failed attempts
        elif n == 9:
            pool.available = False  # lost again once
        elif n == 10:
            pool.available = True  # and back
        elif n >= 11:
            done.set()
            await asyncio.Event().wait()  # park until cancelled
        await asyncio.sleep(0)

    db = Database(make_settings(), lambda s: pool, sleep=fake_sleep)
    await db.start()
    await asyncio.wait_for(done.wait(), timeout=5)
    await db.stop()

    assert delays[:8] == [0.5, 1, 2, 4, 8, 16, 30, 30]
    assert delays[8] == HEALTHY_CHECK_INTERVAL_SECONDS  # healthy: steady interval
    assert delays[9] == 0.5  # attempt counter was reset after success
    assert delays[10] == HEALTHY_CHECK_INTERVAL_SECONDS

    text = caplog.text
    for attempt, delay in enumerate(["0.5", "1.0", "2.0", "4.0", "8.0", "16.0", "30.0", "30.0"], start=1):
        assert f"database not reachable: attempt={attempt} retry_in={delay}s error=PoolTimeout" in text
    assert "database connectivity established after 8 failed attempt(s)" in text
    assert "database connectivity restored after 1 failed attempt(s)" in text


async def test_monitor_never_logs_secrets_from_driver_errors(caplog):
    caplog.set_level(logging.DEBUG)
    pool = FakePool()
    pool.available = True
    pool.fail_queries = True  # OperationalError whose message contains the password
    db = Database(make_settings(DB_RETRY_BASE_SECONDS="0.01", DB_RETRY_MAX_SECONDS="0.02"), lambda s: pool)
    await db.start()
    await asyncio.sleep(0.1)
    await db.stop()
    assert "error=OperationalError" in caplog.text
    assert SECRET_PASSWORD not in caplog.text
    assert "db.internal.example" not in caplog.text


# ---------------------------------------------------------------------------
# Real psycopg_pool against an unreachable server: no blocking, no secrets logged
# ---------------------------------------------------------------------------


def test_real_pool_unreachable_database_end_to_end(caplog):
    caplog.set_level(logging.DEBUG)
    secret_user, secret_db = "secret_user_zz", "secret_db_zz"
    settings = load_settings(
        {
            "DATABASE_URL": make_conninfo(
                host="127.0.0.1", port="1", dbname=secret_db, user=secret_user, password=SECRET_PASSWORD
            ),
            "DB_CONNECT_TIMEOUT_SECONDS": "1",
            "DB_RETRY_BASE_SECONDS": "0.1",
            "DB_RETRY_MAX_SECONDS": "0.2",
            "APP_VERSION": "v1",
            "HOSTNAME": "pod-real",
        }
    )
    app = create_app(settings)  # default factory: the real AsyncConnectionPool

    started = time.monotonic()
    with TestClient(app, raise_server_exceptions=False) as client:
        assert time.monotonic() - started < 2.0, "startup must not block on the database"
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        assert client.get("/loans").status_code == 503
        post = client.post("/loans", json={"borrower_name": "Jane", "loan_amount": 1})
        assert post.status_code == 503
        assert post.headers["x-served-by"] == "pod-real"

    text = caplog.text
    assert "database not reachable: attempt=1" in text
    for secret in (SECRET_PASSWORD, secret_user, secret_db, "127.0.0.1", "port 1"):
        assert secret not in text, f"log leaked {secret!r}"


@pytest.mark.parametrize("cls_name", ["PoolTimeout", "OperationalError"])
def test_503_handler_logs_class_name_only(fake_pool, make_client, caplog, cls_name):
    caplog.set_level(logging.INFO)
    if cls_name == "PoolTimeout":
        fake_pool.available = False
    else:
        fake_pool.fail_queries = True
    client = make_client()
    assert client.get("/loans").status_code == 503
    assert f"database unavailable: path=/loans error={cls_name}" in caplog.text
    assert SECRET_PASSWORD not in caplog.text
