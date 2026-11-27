"""X-Served-By and X-App-Version on every backend response, including errors."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.middleware import HeaderMiddleware

from conftest import make_settings


def _boom() -> None:
    raise RuntimeError("unexpected failure with internal detail")


def _client_with_boom_route(fake_pool, **env) -> TestClient:
    app = create_app(make_settings(**env), pool_factory=lambda s: fake_pool)
    assert isinstance(app, HeaderMiddleware), "the header middleware must wrap the finished app"
    app.app.add_api_route("/boom", _boom)  # test-only route on the inner FastAPI app
    return TestClient(app, raise_server_exceptions=False)


def _request(client: TestClient, fake_pool, status: int):
    if status == 200:
        return client.get("/healthz")
    if status == 404:
        return client.get("/does-not-exist")
    if status == 405:
        return client.delete("/loans")
    if status == 422:
        return client.post("/loans", json={"loan_amount": "abc"})
    if status == 503:
        fake_pool.available = False
        return client.get("/loans")
    if status == 500:
        return client.get("/boom")
    raise AssertionError(status)


@pytest.mark.parametrize("status", [200, 404, 405, 422, 503, 500])
def test_both_headers_present_on_every_status(fake_pool, status):
    with _client_with_boom_route(fake_pool) as client:
        response = _request(client, fake_pool, status)
    assert response.status_code == status
    assert response.headers["x-served-by"] == "pod-abc"
    assert response.headers["x-app-version"] == "v1"
    # exactly one of each, never duplicated
    assert len(response.headers.get_list("x-served-by")) == 1
    assert len(response.headers.get_list("x-app-version")) == 1


def test_unexpected_500_has_generic_body_and_both_headers(fake_pool):
    with _client_with_boom_route(fake_pool) as client:
        response = client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error"}
    assert "internal detail" not in response.text
    assert response.headers["x-served-by"] == "pod-abc"
    assert response.headers["x-app-version"] == "v1"


def test_headers_on_readiness_success_and_failure(fake_pool, client):
    ok = client.get("/readyz")
    fake_pool.available = False
    bad = client.get("/readyz")
    for response in (ok, bad):
        assert response.headers["x-served-by"] == "pod-abc"
        assert response.headers["x-app-version"] == "v1"
    assert (ok.status_code, bad.status_code) == (200, 503)


def test_app_version_changes_from_v1_to_v2_through_configuration(make_client):
    v1 = make_client(APP_VERSION="v1")
    assert v1.get("/healthz").headers["x-app-version"] == "v1"
    assert v1.get("/healthz").json()["version"] == "v1"

    v2 = make_client(APP_VERSION="v2")  # new process/pod with the new env value
    assert v2.get("/healthz").headers["x-app-version"] == "v2"
    assert v2.get("/healthz").json()["version"] == "v2"
    assert v2.get("/loans").headers["x-app-version"] == "v2"


def test_served_by_comes_from_hostname(make_client):
    client = make_client(HOSTNAME="backend-5f7c9d-k2x8p")
    response = client.get("/loans")
    assert response.headers["x-served-by"] == "backend-5f7c9d-k2x8p"
    assert client.get("/healthz").json()["served_by"] == "backend-5f7c9d-k2x8p"


def test_missing_hostname_and_version_fall_back_to_unknown(make_client):
    response = make_client(HOSTNAME="", APP_VERSION="").get("/healthz")
    assert response.headers["x-served-by"] == "unknown"
    assert response.headers["x-app-version"] == "unknown"


async def test_middleware_replaces_existing_values_and_ignores_non_http_scopes():
    sent = []

    async def inner(scope, receive, send):
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": [(b"x-app-version", b"stale")]})
            await send({"type": "http.response.body", "body": b""})
        else:
            sent.append(("passthrough", scope["type"]))

    async def send(message):
        sent.append(message)

    mw = HeaderMiddleware(inner, served_by="pod-1", app_version="v9")
    await mw({"type": "http"}, None, send)
    await mw({"type": "lifespan"}, None, send)

    headers = sent[0]["headers"]
    assert headers.count((b"x-app-version", b"v9")) == 1
    assert (b"x-app-version", b"stale") not in headers
    assert (b"x-served-by", b"pod-1") in headers
    assert ("passthrough", "lifespan") in sent
