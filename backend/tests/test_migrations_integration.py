"""Migration files run against a real PostgreSQL: fresh init and seed idempotency.

Each test works in a throwaway schema inside a transaction that is always rolled
back, so the real `loans` table and its sequence are never changed.
Skipped unless TEST_DATABASE_URL is set (see test_api_integration.py).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"
CREATE_SQL = MIGRATIONS / "001_create_loans.sql"
SEED_SQL = MIGRATIONS / "002_seed_loans.sql"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set (needs PostgreSQL on localhost:5433)"),
]


@pytest.fixture
def scratch():
    """Connection whose search_path points at a new empty schema; everything is rolled back."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.transaction(force_rollback=True):
            schema = f"migration_test_{uuid.uuid4().hex[:12]}"
            conn.execute(f'CREATE SCHEMA "{schema}"')
            conn.execute(f'SET LOCAL search_path TO "{schema}"')
            yield conn


def _run(conn: psycopg.Connection, sql: str) -> int:
    return conn.execute(sql).rowcount


def _count(conn: psycopg.Connection, where: str = "TRUE") -> int:
    return conn.execute(f"SELECT count(*) FROM loans WHERE {where}").fetchone()[0]


def test_migration_files_exist_in_execution_order():
    names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert names == ["001_create_loans.sql", "002_seed_loans.sql"]


def test_fresh_initialization_creates_table_and_seeds(scratch):
    _run(scratch, CREATE_SQL.read_text(encoding="utf-8"))
    inserted = _run(scratch, SEED_SQL.read_text(encoding="utf-8"))
    assert inserted >= 5
    assert _count(scratch) == inserted
    assert _count(scratch, "status = 'PENDING'") >= 1
    ids = [r[0] for r in scratch.execute("SELECT id FROM loans ORDER BY id")]
    assert ids == list(range(1, inserted + 1))  # ids follow the seed list order


def test_running_both_migrations_again_adds_nothing(scratch):
    _run(scratch, CREATE_SQL.read_text(encoding="utf-8"))
    first = _run(scratch, SEED_SQL.read_text(encoding="utf-8"))
    _run(scratch, CREATE_SQL.read_text(encoding="utf-8"))  # IF NOT EXISTS: no error
    assert _run(scratch, SEED_SQL.read_text(encoding="utf-8")) == 0
    assert _run(scratch, SEED_SQL.read_text(encoding="utf-8")) == 0
    assert _count(scratch) == first


def _seed_with_null_city(seed_sql: str) -> str:
    # The shipped seeds all have a city, so make one NULL to exercise the null-safe match.
    assert "'Chennai'" in seed_sql, "seed file changed: update this test's NULL substitution"
    return seed_sql.replace("'Chennai'", "NULL", 1)


def test_seed_with_null_city_is_not_duplicated(scratch):
    seed = _seed_with_null_city(SEED_SQL.read_text(encoding="utf-8"))
    _run(scratch, CREATE_SQL.read_text(encoding="utf-8"))
    first = _run(scratch, seed)
    assert _run(scratch, seed) == 0
    assert _count(scratch) == first
    assert _count(scratch, "property_city IS NULL") == 1


def test_plain_equality_would_duplicate_null_city_rows(scratch):
    """Control: proves the previous test can fail. With '=' a NULL city never matches."""
    seed = _seed_with_null_city(SEED_SQL.read_text(encoding="utf-8"))
    assert "IS NOT DISTINCT FROM" in seed
    unsafe = seed.replace("IS NOT DISTINCT FROM", "=")
    _run(scratch, CREATE_SQL.read_text(encoding="utf-8"))
    first = _run(scratch, unsafe)
    assert _run(scratch, unsafe) == 1  # the NULL-city row is inserted again
    assert _count(scratch) == first + 1


def test_seed_against_already_seeded_real_table_inserts_nothing():
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.transaction(force_rollback=True):
            before = conn.execute("SELECT count(*) FROM public.loans").fetchone()[0]
            assert before >= 5, "expected the initialised test database"
            conn.execute("SET LOCAL search_path TO public")
            assert _run(conn, SEED_SQL.read_text(encoding="utf-8")) == 0
            assert conn.execute("SELECT count(*) FROM public.loans").fetchone()[0] == before
