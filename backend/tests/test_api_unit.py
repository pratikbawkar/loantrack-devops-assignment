"""API behaviour without a database (FakePool)."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.loans import INSERT_LOAN_SQL

LOAN_FIELDS = {"id", "borrower_name", "loan_amount", "property_city", "status", "created_at"}


# ---------------------------------------------------------------------------
# GET /loans
# ---------------------------------------------------------------------------


def test_get_loans_returns_array_of_loan_objects(client):
    response = client.get("/loans")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list) and len(body) == 5
    for loan in body:
        assert set(loan) == LOAN_FIELDS
        assert isinstance(loan["id"], int)
        assert isinstance(loan["loan_amount"], str)
        datetime.fromisoformat(loan["created_at"].replace("Z", "+00:00"))


def test_get_loans_newest_first(client):
    ids = [loan["id"] for loan in client.get("/loans").json()]
    assert ids == sorted(ids, reverse=True)


def test_get_loans_amount_is_string_with_two_decimals(client):
    by_name = {loan["borrower_name"]: loan for loan in client.get("/loans").json()}
    assert by_name["Alice Morgan"]["loan_amount"] == "250000.00"
    assert by_name["Ben Carter"]["loan_amount"] == "180500.50"
    assert by_name["Emma Lindqvist"]["property_city"] is None


def test_get_loans_empty_table_returns_empty_array(fake_pool, client):
    fake_pool.rows.clear()
    response = client.get("/loans")
    assert (response.status_code, response.json()) == (200, [])


# ---------------------------------------------------------------------------
# POST /loans
# ---------------------------------------------------------------------------


def test_post_creates_loan_with_pending_status(client):
    response = client.post(
        "/loans", json={"borrower_name": "Jane Doe", "loan_amount": 150000.5, "property_city": "Austin"}
    )
    assert response.status_code == 201
    loan = response.json()
    assert set(loan) == LOAN_FIELDS
    assert loan["status"] == "PENDING"
    assert loan["loan_amount"] == "150000.50"
    assert (loan["borrower_name"], loan["property_city"]) == ("Jane Doe", "Austin")
    assert client.get("/loans").json()[0]["id"] == loan["id"]  # newest first


def test_post_always_pending_and_status_never_sent_to_sql(fake_pool, client):
    client.post("/loans", json={"borrower_name": "A", "loan_amount": "1"})
    (sql, params), = fake_pool.inserts()
    assert "'PENDING'" in sql
    assert "status" not in params
    assert "'PENDING'" in INSERT_LOAN_SQL


@pytest.mark.parametrize("status", ["APPROVED", "PENDING", "REJECTED", "", None])
def test_client_supplied_status_returns_422(fake_pool, client, status):
    response = client.post(
        "/loans", json={"borrower_name": "Jane", "loan_amount": "100.00", "status": status}
    )
    assert response.status_code == 422
    assert any(err["loc"][-1] == "status" for err in response.json()["detail"])
    assert fake_pool.inserts() == []


def test_unknown_extra_field_returns_422(fake_pool, client):
    response = client.post("/loans", json={"borrower_name": "J", "loan_amount": 1, "id": 99})
    assert response.status_code == 422
    assert fake_pool.inserts() == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"loan_amount": 100},  # missing name
        {"borrower_name": "Jane"},  # missing amount
        {"borrower_name": "", "loan_amount": 100},
        {"borrower_name": "   ", "loan_amount": 100},
        {"borrower_name": "x" * 201, "loan_amount": 100},
        {"borrower_name": "Jane", "loan_amount": 0},
        {"borrower_name": "Jane", "loan_amount": -5},
        {"borrower_name": "Jane", "loan_amount": "12.345"},  # 3 decimal places
        {"borrower_name": "Jane", "loan_amount": "1000000000000.00"},  # > NUMERIC(14,2)
        {"borrower_name": "Jane", "loan_amount": "abc"},
        {"borrower_name": "Jane", "loan_amount": "NaN"},
        {"borrower_name": "Jane", "loan_amount": "Infinity"},
        {"borrower_name": "Jane", "loan_amount": 100, "property_city": "c" * 101},
    ],
)
def test_invalid_payloads_return_422_and_insert_nothing(fake_pool, client, payload):
    response = client.post("/loans", json=payload)
    assert response.status_code == 422
    assert fake_pool.inserts() == []


def test_malformed_json_returns_422(fake_pool, client):
    response = client.post("/loans", content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert fake_pool.inserts() == []


def test_maximum_amount_is_accepted(client):
    response = client.post("/loans", json={"borrower_name": "Max", "loan_amount": "999999999999.99"})
    assert response.status_code == 201
    assert response.json()["loan_amount"] == "999999999999.99"


def test_amount_accepts_string_or_number(client):
    assert client.post("/loans", json={"borrower_name": "S", "loan_amount": "1234.5"}).json()["loan_amount"] == "1234.50"
    assert client.post("/loans", json={"borrower_name": "N", "loan_amount": 99}).json()["loan_amount"] == "99.00"


def test_name_and_city_are_trimmed_and_blank_city_is_null(client):
    loan = client.post(
        "/loans", json={"borrower_name": "  Jane  ", "loan_amount": 1, "property_city": "   "}
    ).json()
    assert loan["borrower_name"] == "Jane"
    assert loan["property_city"] is None


def test_city_is_optional(client):
    loan = client.post("/loans", json={"borrower_name": "Jane", "loan_amount": 1}).json()
    assert loan["property_city"] is None


# ---------------------------------------------------------------------------
# /healthz and /readyz bodies (A3: served_by and version included)
# ---------------------------------------------------------------------------


def test_healthz_body(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "served_by": "pod-abc", "version": "v1"}


def test_readyz_body_when_database_reachable(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok", "served_by": "pod-abc", "version": "v1"}


def test_readyz_runs_select_1_through_pool(fake_pool, client):
    before = len(fake_pool.executed)
    client.get("/readyz")
    assert ("SELECT 1", None) in fake_pool.executed[before:]


def test_readyz_times_out_to_503_when_database_hangs(fake_pool, client):
    fake_pool.hang = True
    response = client.get("/readyz")
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Text that PostgreSQL cannot store: NUL and lone surrogates -> 422, not 500
# ---------------------------------------------------------------------------

# Raw JSON escapes, sent as bytes: a lone surrogate cannot be produced by a
# client-side JSON encoder that writes UTF-8, but it is valid JSON text.
# Value: (escaped text, expected Pydantic error type).
# NUL is rejected by our validator; lone surrogates by Pydantic's own str check.
UNSTORABLE_TEXT = {
    "nul_middle": ("a\\u0000b", "value_error"),
    "nul_only": ("\\u0000", "value_error"),
    "nul_end": ("Jane\\u0000", "value_error"),
    "high_surrogate": ("a\\ud800b", "string_unicode"),
    "low_surrogate": ("a\\udfffb", "string_unicode"),
    "reversed_pair": ("\\udc00\\ud800", "string_unicode"),
}


def _post_raw(client, field: str, escaped_text: str, loan_amount: str = '"100.00"'):
    fields = {"borrower_name": '"Jane"', "loan_amount": loan_amount, "property_city": '"Pune"'}
    fields[field] = f'"{escaped_text}"'
    body = "{" + ",".join(f'"{k}": {v}' for k, v in fields.items()) + "}"
    return client.post("/loans", content=body.encode("ascii"), headers={"content-type": "application/json"})


@pytest.mark.parametrize("field", ["borrower_name", "property_city"])
@pytest.mark.parametrize(("escaped_text", "error_type"), UNSTORABLE_TEXT.values(), ids=UNSTORABLE_TEXT.keys())
def test_unstorable_text_returns_422_and_inserts_nothing(fake_pool, client, caplog, field, escaped_text, error_type):
    response = _post_raw(client, field, escaped_text)
    assert response.status_code == 422
    assert response.headers["x-served-by"] == "pod-abc"
    assert response.headers["x-app-version"] == "v1"
    errors = response.json()["detail"]  # body is valid JSON despite echoing the bad input
    field_errors = [e for e in errors if e["loc"] == ["body", field]]
    assert [e["type"] for e in field_errors] == [error_type], errors
    if error_type == "value_error":
        assert "NUL" in field_errors[0]["msg"]
    assert fake_pool.inserts() == []
    assert "UnicodeEncodeError" not in caplog.text and "unhandled error" not in caplog.text


@pytest.mark.parametrize(("escaped_text", "_type"), UNSTORABLE_TEXT.values(), ids=UNSTORABLE_TEXT.keys())
def test_422_with_unstorable_input_and_other_errors_is_still_422(fake_pool, client, escaped_text, _type):
    # Regression: echoing a lone surrogate in ANY 422 body used to crash the
    # response encoder and turn the 422 into a 500.
    response = _post_raw(client, "borrower_name", escaped_text, loan_amount="0")
    assert response.status_code == 422
    locs = {tuple(e["loc"]) for e in response.json()["detail"]}
    assert ("body", "loan_amount") in locs
    assert fake_pool.inserts() == []


def test_422_body_is_ascii_encoded_with_same_structure(client):
    response = client.post("/loans", json={"borrower_name": "Zoë", "loan_amount": 0})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    response.content.decode("ascii")  # non-ASCII is \u-escaped, never raw bytes
    (error,) = response.json()["detail"]
    assert set(error) >= {"type", "loc", "msg", "input"}  # FastAPI's default error shape
    assert error["loc"] == ["body", "loan_amount"]


VALID_UNICODE = [
    ("Zoë Ångström", "São Paulo"),
    ("José Núñez", "Zürich"),
    ("राहुल शर्मा", "पुणे"),
    ("李小龙", "北京"),
    ("Ølaf 🏠", "Kraków"),  # emoji is a valid (paired) astral character
    ("O'Brien-Smith", None),
]


@pytest.mark.parametrize(("name", "city"), VALID_UNICODE)
def test_normal_unicode_names_and_cities_still_work(fake_pool, client, name, city):
    response = client.post("/loans", json={"borrower_name": name, "loan_amount": "10.00", "property_city": city})
    assert response.status_code == 201
    loan = response.json()
    assert (loan["borrower_name"], loan["property_city"]) == (name, city)
    (_, params), = fake_pool.inserts()
    assert (params["borrower_name"], params["property_city"]) == (name, city)
