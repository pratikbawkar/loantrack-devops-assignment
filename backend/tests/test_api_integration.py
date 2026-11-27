"""Integration tests against a real PostgreSQL with the migrations applied.

Skipped unless TEST_DATABASE_URL is set, for example (dummy credentials):
    TEST_DATABASE_URL=postgresql://loantrack_user:change_me_dummy_password@localhost:5433/loantrack

A separate variable (not DATABASE_URL) is used so the tests never run against
a database by accident.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set (needs PostgreSQL on localhost:5433)"),
]


@pytest.fixture
def client():
    settings = load_settings({"DATABASE_URL": TEST_DATABASE_URL, "APP_VERSION": "v-int", "HOSTNAME": "pod-int"})
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def marker():
    """Unique borrower name; rows created with it are deleted afterwards."""
    name = f"integration-{uuid.uuid4().hex[:12]}"
    yield name
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        conn.execute("DELETE FROM loans WHERE borrower_name = %s", (name,))


def _db_count(name: str | None = None) -> int:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        if name is None:
            return conn.execute("SELECT count(*) FROM loans").fetchone()[0]
        return conn.execute("SELECT count(*) FROM loans WHERE borrower_name = %s", (name,)).fetchone()[0]


def test_schema_matches_specification():
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_name = 'loans' ORDER BY ordinal_position
            """
        ).fetchall()
    cols = {r[0]: r[1:] for r in rows}
    assert list(cols) == ["id", "borrower_name", "loan_amount", "property_city", "status", "created_at"]
    assert cols["id"][0] == "integer" and "nextval" in cols["id"][2]
    assert cols["borrower_name"][:2] == ("text", "NO")
    assert cols["loan_amount"][0] == "numeric" and cols["loan_amount"][3:] == (14, 2)
    assert cols["loan_amount"][1] == "NO"
    assert cols["property_city"][:2] == ("text", "YES")
    assert cols["status"][:2] == ("text", "NO") and "'PENDING'" in cols["status"][2]
    assert cols["created_at"][:2] == ("timestamp with time zone", "NO") and "now()" in cols["created_at"][2]


def test_database_default_status_is_pending(marker):
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        status = conn.execute(
            "INSERT INTO loans (borrower_name, loan_amount) VALUES (%s, 1) RETURNING status", (marker,)
        ).fetchone()[0]
    assert status == "PENDING"


def test_readyz_200_with_real_database(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"
    assert response.headers["x-served-by"] == "pod-int"
    assert response.headers["x-app-version"] == "v-int"


def test_seed_data_has_at_least_five_rows(client):
    response = client.get("/loans")
    assert response.status_code == 200
    loans = response.json()
    assert isinstance(loans, list) and len(loans) >= 5
    assert {"id", "borrower_name", "loan_amount", "property_city", "status", "created_at"} == set(loans[0])


def test_post_then_get_returns_new_pending_row(client, marker):
    created = client.post("/loans", json={"borrower_name": marker, "loan_amount": "4321.5", "property_city": "Pune"})
    assert created.status_code == 201
    loan = created.json()
    assert loan["status"] == "PENDING" and loan["loan_amount"] == "4321.50"

    listed = client.get("/loans").json()
    assert listed[0]["id"] == loan["id"]  # newest first
    assert _db_count(marker) == 1  # really stored in PostgreSQL


def test_client_status_rejected_and_nothing_stored(client, marker):
    before = _db_count()
    response = client.post("/loans", json={"borrower_name": marker, "loan_amount": 10, "status": "APPROVED"})
    assert response.status_code == 422
    assert _db_count() == before
    assert _db_count(marker) == 0


@pytest.mark.parametrize("escaped", ["\\u0000", "\\ud800"], ids=["nul", "lone_surrogate"])
@pytest.mark.parametrize("field", ["borrower_name", "property_city"])
def test_unstorable_text_is_422_against_real_database(client, marker, field, escaped):
    # Before the fix both were HTTP 500: NUL failed inside PostgreSQL, and the
    # lone surrogate's 422 body could not be encoded.
    fields = {"borrower_name": f'"{marker}"', "loan_amount": '"5.00"', "property_city": '"Pune"'}
    fields[field] = f'"{marker}{escaped}x"'
    body = "{" + ",".join(f'"{k}": {v}' for k, v in fields.items()) + "}"
    before = _db_count()
    response = client.post("/loans", content=body.encode("ascii"), headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.headers["x-app-version"] == "v-int"
    assert _db_count() == before


def test_unicode_round_trips_through_postgresql(client, marker):
    name, city = f"{marker} Zoë राहुल 李 🏠", "São Paulo"
    try:
        created = client.post("/loans", json={"borrower_name": name, "loan_amount": "1.00", "property_city": city})
        assert created.status_code == 201
        listed = {loan["id"]: loan for loan in client.get("/loans").json()}
        stored = listed[created.json()["id"]]
        assert (stored["borrower_name"], stored["property_city"]) == (name, city)
    finally:
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            conn.execute("DELETE FROM loans WHERE borrower_name = %s", (name,))
