from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app as core_app
from collector_app.main import app as collector_app

_REQUIRED_HEADERS = {
    "cache-control": "no-store, no-cache, must-revalidate, max-age=0",
    "pragma": "no-cache",
    "expires": "0",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


def _assert_no_store(client: TestClient, path: str = "/healthz") -> None:
    response = client.get(path)
    assert response.status_code == 200
    for name, value in _REQUIRED_HEADERS.items():
        assert response.headers[name] == value
    assert "etag" not in response.headers


def test_core_and_collector_responses_are_no_store() -> None:
    with TestClient(core_app) as core_client:
        _assert_no_store(core_client)
    with TestClient(collector_app) as collector_client:
        _assert_no_store(collector_client)


def test_core_cors_is_explicit_and_preflight_is_no_store() -> None:
    with TestClient(core_app) as client:
        response = client.options(
            "/healthz",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert response.headers["cache-control"].startswith("no-store")