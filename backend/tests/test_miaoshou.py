from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.runtime import CommerceRuntime
from app.integrations.miaoshou.client import (
    MiaoshouClient,
    MiaoshouClientError,
    MiaoshouConfig,
    MiaoshouFailure,
    MiaoshouFailureCategory,
    classify_business_failure,
    miaoshou_enabled_from_env,
)
from app.integrations.miaoshou.shops import MiaoshouShopAdapter
from app.integrations.miaoshou.signing import (
    compact_json_body,
    prepare_signed_request,
    sign_request,
)
from app.main import create_app
from app.use_cases.miaoshou_shops import MiaoshouShop, MiaoshouShopPage, MiaoshouShopQueryService
from shared.safe_paths import PROJECT_ROOT

_ADMIN_SECRET = "local-admin-secret-with-at-least-32-characters"


def _ephemeral_credentials() -> tuple[str, str]:
    return secrets.token_urlsafe(24), secrets.token_urlsafe(32)


class _FakeProvider:
    def __init__(self, page: MiaoshouShopPage | None = None, failure: Exception | None = None) -> None:
        self.page = page
        self.failure = failure
        self.queries: list[Any] = []

    async def query_shops(self, query):
        self.queries.append(query)
        if self.failure is not None:
            raise self.failure
        assert self.page is not None
        return self.page


@pytest.fixture
def miaoshou_api_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any]]:
    database_path = PROJECT_ROOT / "data" / f"test-miaoshou-api-{uuid4()}.sqlite3"
    monkeypatch.setenv("CORE_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ADMIN_BOOTSTRAP_SECRET", _ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("MIAOSHOU_ENABLED", "false")
    monkeypatch.setenv("MIAOSHOU_APP_KEY", "")
    monkeypatch.setenv("MIAOSHOU_APP_SECRET", "")
    monkeypatch.delenv("APP_MASTER_KEY", raising=False)
    monkeypatch.delenv("TIKTOK_APP_KEY", raising=False)
    monkeypatch.delenv("TIKTOK_APP_SECRET", raising=False)
    application = create_app()
    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            yield client, application
    finally:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{database_path}{suffix}")
            if path.exists():
                path.unlink()


def _login(client: TestClient) -> str:
    response = client.post("/api/session", json={"bootstrap_secret": _ADMIN_SECRET})
    assert response.status_code == 201
    return response.json()["csrf_token"]


def test_signing_is_deterministic_and_uses_exact_sorted_body() -> None:
    app_key, app_secret = _ephemeral_credentials()
    payload = {"pageSize": 100, "site": "US", "platform": "tiktok", "pageNo": 1}
    body = compact_json_body(payload)
    request_one = prepare_signed_request(
        app_key=app_key,
        app_secret=app_secret,
        path="/open/v1/product/shop/shop/get_shop_list",
        timestamp=1_800_000_000,
        payload=payload,
    )
    request_two = prepare_signed_request(
        app_key=app_key,
        app_secret=app_secret,
        path="/open/v1/product/shop/shop/get_shop_list",
        timestamp=1_800_000_000,
        payload=payload,
    )
    assert body == b'{"pageNo":1,"pageSize":100,"platform":"tiktok","site":"US"}'
    assert request_one.body == request_two.body == body
    assert request_one.headers["x-sign"] == request_two.headers["x-sign"]
    assert request_one.headers["x-timestamp"] == "1800000000"
    assert sign_request(
        app_secret=app_secret,
        path=request_one.path,
        timestamp=1_800_000_000,
        app_key=app_key,
        body=body,
    ) == request_one.headers["x-sign"]
    assert app_key not in repr(MiaoshouConfig(app_key=app_key, app_secret=app_secret))
    assert app_secret not in repr(MiaoshouConfig(app_key=app_key, app_secret=app_secret))


def test_configuration_is_fail_closed_and_provider_is_opt_in() -> None:
    app_key, app_secret = _ephemeral_credentials()
    assert miaoshou_enabled_from_env({}) is False
    with pytest.raises(ValueError) as missing:
        MiaoshouConfig.from_env({"MIAOSHOU_ENABLED": "true"})
    assert str(missing.value) == "BLOCKED_LIVE_CREDENTIALS"
    config = MiaoshouConfig.from_env(
        {
            "MIAOSHOU_APP_KEY": app_key,
            "MIAOSHOU_APP_SECRET": app_secret,
            "MIAOSHOU_BASE_URL": "https://openapi-erp.91miaoshou.com",
        }
    )
    assert config.base_url == "https://openapi-erp.91miaoshou.com"

    with pytest.raises(ValueError):
        miaoshou_enabled_from_env({"MIAOSHOU_ENABLED": "sometimes"})
    with pytest.raises(ValueError):
        MiaoshouConfig.from_env(
            {
                "MIAOSHOU_APP_KEY": app_key,
                "MIAOSHOU_APP_SECRET": app_secret,
                "MIAOSHOU_BASE_URL": "http://127.0.0.1:9000",
            }
        )


def test_error_codes_are_normalized_without_preserving_messages() -> None:
    assert classify_business_failure("signInvalid").category is MiaoshouFailureCategory.AUTHORIZATION
    assert classify_business_failure("appNoPermission").category is MiaoshouFailureCategory.PERMISSION
    assert classify_business_failure("accountQpsRateLimit").category is MiaoshouFailureCategory.RATE_LIMITED
    assert classify_business_failure("unknownBusinessCode").category is MiaoshouFailureCategory.VALIDATION


@pytest.mark.asyncio
async def test_client_classifies_empty_and_business_error_responses() -> None:
    app_key, app_secret = _ephemeral_credentials()

    async def empty_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    empty_client = MiaoshouClient(
        MiaoshouConfig(app_key=app_key, app_secret=app_secret),
        transport=httpx.MockTransport(empty_handler),
    )
    with pytest.raises(MiaoshouClientError) as empty_failure:
        await empty_client.post("/open/v1/product/shop/shop/get_shop_list", {"site": "US"})
    assert empty_failure.value.failure.category is MiaoshouFailureCategory.INVALID_RESPONSE

    async def auth_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": "fail", "code": "signInvalid", "message": "sensitive upstream detail"},
        )

    auth_client = MiaoshouClient(
        MiaoshouConfig(app_key=app_key, app_secret=app_secret),
        transport=httpx.MockTransport(auth_handler),
    )
    with pytest.raises(MiaoshouClientError) as auth_failure:
        await auth_client.post("/open/v1/product/shop/shop/get_shop_list", {"site": "US"})
    assert auth_failure.value.failure.category is MiaoshouFailureCategory.AUTHORIZATION
    assert "sensitive upstream detail" not in str(auth_failure.value)
    assert app_secret not in str(auth_failure.value)


@pytest.mark.asyncio
async def test_shop_adapter_normalizes_documented_shop_fields() -> None:
    app_key, app_secret = _ephemeral_credentials()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": "success",
                "data": {
                    "shopList": [
                        {
                            "shopId": 123,
                            "shopNick": "US shop",
                            "platform": "tiktok",
                            "site": "US",
                            "siteName": "United States",
                            "status": "normal",
                            "gmtExpire": "2030-01-01 00:00:00",
                            "gmtLastAuth": "2026-01-01 00:00:00",
                            "parentShopId": 99,
                            "isCb": 1,
                            "isCnsc": 0,
                        }
                    ]
                },
            },
        )

    service = MiaoshouShopQueryService(
        MiaoshouShopAdapter(
            MiaoshouClient(
                MiaoshouConfig(app_key=app_key, app_secret=app_secret),
                transport=httpx.MockTransport(handler),
            )
        )
    )
    page = await service.query(platform="tiktok", site="US", page_size=100)
    assert page.items[0] == MiaoshouShop(
        shop_id="123",
        shop_name="US shop",
        platform="tiktok",
        site="US",
        site_name="United States",
        status="normal",
        authorization_expires_at="2030-01-01 00:00:00",
        last_authorized_at="2026-01-01 00:00:00",
        parent_shop_id="99",
        is_cross_border=True,
        is_global=False,
    )
    with pytest.raises(ValueError):
        await service.query(platform="tiktok", site="RU")


def test_miaoshou_routes_are_protected_and_fail_closed(
    miaoshou_api_client: tuple[TestClient, Any],
) -> None:
    client, application = miaoshou_api_client
    denied = client.get("/api/miaoshou/capabilities")
    assert denied.status_code == 401
    csrf = _login(client)
    capabilities = client.get("/api/miaoshou/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json() == {
        "provider": "miaoshou",
        "configured": False,
        "shop_query_enabled": False,
        "blockers": ["MIAOSHOU_PROVIDER_DISABLED"],
    }
    blocked = client.get("/api/miaoshou/shops?platform=tiktok&site=US")
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "MIAOSHOU_PROVIDER_DISABLED"
    assert client.get("/api/miaoshou/shops?platform=tiktok&site=US", headers={"X-CSRF-Token": csrf}).status_code == 503

    runtime: CommerceRuntime = application.state.commerce_runtime
    application.state.commerce_runtime = replace(
        runtime,
        miaoshou_shop_service=None,
        miaoshou_configured=False,
        miaoshou_blocker="BLOCKED_LIVE_CREDENTIALS",
    )
    missing_credentials = client.get("/api/miaoshou/capabilities")
    assert missing_credentials.status_code == 200
    assert missing_credentials.json()["blockers"] == ["BLOCKED_LIVE_CREDENTIALS"]
    blocked_credentials = client.get("/api/miaoshou/shops?platform=tiktok&site=US")
    assert blocked_credentials.status_code == 503
    assert blocked_credentials.json()["error"] == {
        "code": "BLOCKED_LIVE_CREDENTIALS",
        "message": "Miaoshou live credentials are not configured",
        "request_id": blocked_credentials.json()["error"]["request_id"],
    }


def test_miaoshou_route_returns_only_normalized_read_data(
    miaoshou_api_client: tuple[TestClient, Any],
) -> None:
    client, application = miaoshou_api_client
    _login(client)
    runtime: CommerceRuntime = application.state.commerce_runtime
    provider = _FakeProvider(
        page=MiaoshouShopPage(
            items=(
                MiaoshouShop(
                    shop_id="shop-1",
                    shop_name="Read-only shop",
                    platform="tiktok",
                    site="US",
                    site_name="United States",
                    status="normal",
                    authorization_expires_at=None,
                    last_authorized_at=None,
                    parent_shop_id=None,
                    is_cross_border=True,
                    is_global=False,
                ),
            ),
            page_no=1,
            page_size=100,
            next_page_no=None,
        )
    )
    replacement = replace(
        runtime,
        miaoshou_shop_service=MiaoshouShopQueryService(provider),
        miaoshou_configured=True,
        miaoshou_blocker="",
    )
    application.state.commerce_runtime = replacement
    response = client.get("/api/miaoshou/shops?platform=tiktok&site=US")
    assert response.status_code == 200
    assert response.json()["items"][0] == {
        "shop_id": "shop-1",
        "shop_name": "Read-only shop",
        "platform": "tiktok",
        "site": "US",
        "site_name": "United States",
        "status": "normal",
        "authorization_expires_at": None,
        "last_authorized_at": None,
        "parent_shop_id": None,
        "is_cross_border": True,
        "is_global": False,
    }
    assert provider.queries[0].site == "US"
    assert "access_token" not in response.text
    assert "app_secret" not in response.text

    global_provider = _FakeProvider(
        page=MiaoshouShopPage(
            items=(
                MiaoshouShop(
                    shop_id="global-shop-1",
                    shop_name="Read-only global shop",
                    platform="tiktokGlobal",
                    site="TIKTOKGLOBALUS",
                    site_name="TikTok Global US",
                    status="normal",
                    authorization_expires_at=None,
                    last_authorized_at=None,
                    parent_shop_id="global-parent-1",
                    is_cross_border=True,
                    is_global=True,
                ),
            ),
            page_no=1,
            page_size=100,
            next_page_no=None,
        )
    )
    application.state.commerce_runtime = replace(
        replacement,
        miaoshou_shop_service=MiaoshouShopQueryService(global_provider),
    )
    global_response = client.get(
        "/api/miaoshou/shops?platform=tiktokGlobal&site=TIKTOKGLOBALUS"
    )
    assert global_response.status_code == 200
    assert global_response.json()["items"][0]["platform"] == "tiktokGlobal"
    assert global_response.json()["items"][0]["is_global"] is True
    assert global_provider.queries[0].platform == "tiktokGlobal"
    assert global_provider.queries[0].site == "TIKTOKGLOBALUS"


def test_miaoshou_upstream_failure_is_redacted(
    miaoshou_api_client: tuple[TestClient, Any],
) -> None:
    client, application = miaoshou_api_client
    _login(client)
    runtime: CommerceRuntime = application.state.commerce_runtime
    provider = _FakeProvider(
        failure=MiaoshouClientError(
            MiaoshouFailure(MiaoshouFailureCategory.AUTHORIZATION, code="signInvalid")
        )
    )
    application.state.commerce_runtime = replace(
        runtime,
        miaoshou_shop_service=MiaoshouShopQueryService(provider),
        miaoshou_configured=True,
        miaoshou_blocker="",
    )
    response = client.get("/api/miaoshou/shops?platform=tiktok&site=US")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "MIAOSHOU_AUTHORIZATION_BLOCKED"
    assert "signInvalid" not in response.text