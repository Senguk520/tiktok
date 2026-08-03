from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.dependencies import commerce_runtime, shop_access_context
from app.db.models import AdminSession, ProductDraft
from app.domain.enums import AuthorizationStatus, ListingMode, ProductDraftStatus, Scope
from app.domain.orders import NormalizedOrder, NormalizedOrderLine, NormalizedOrderPage
from app.domain.product_payload import normalized_product_to_payload
from app.domain.scopes import ScopeSet
from app.integrations.tiktok.orders import OrderGatewayError
from app.main import create_app
from app.use_cases.commerce_context import ShopAccessContext
from shared.safe_paths import PROJECT_ROOT

_ADMIN_SECRET = "local-admin-secret-with-at-least-32-characters"
_SHOP_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, object]]:
    database_path = PROJECT_ROOT / "data" / f"test-core-api-{uuid4()}.sqlite3"
    monkeypatch.setenv("CORE_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ADMIN_BOOTSTRAP_SECRET", _ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "false")
    monkeypatch.delenv("APP_MASTER_KEY", raising=False)
    monkeypatch.delenv("TIKTOK_APP_KEY", raising=False)
    monkeypatch.delenv("TIKTOK_APP_SECRET", raising=False)
    test_app = create_app()
    try:
        with TestClient(test_app, raise_server_exceptions=False) as client:
            yield client, test_app
    finally:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{database_path}{suffix}")
            if path.exists():
                path.unlink()


def _login(client: TestClient) -> tuple[str, str]:
    response = client.post("/api/session", json={"bootstrap_secret": _ADMIN_SECRET})
    assert response.status_code == 201
    body = response.json()
    return body["csrf_token"], client.cookies["tiktok_admin_session"]


def _context() -> ShopAccessContext:
    return ShopAccessContext(
        shop_binding_id=_SHOP_ID,
        shop_id="platform-shop-1",
        region="MY",
        listing_mode=ListingMode.LOCAL_REPLICATION,
        authorization_status=AuthorizationStatus.ACTIVE,
        scopes=ScopeSet(frozenset({Scope.ORDER_INFO, Scope.PRODUCT_BASIC})),
        access_token="upstream-access-token",
        shop_cipher="upstream-shop-cipher",
    )


def _draft_payload() -> dict[str, object]:
    return {
        "title": "Lamp",
        "description": "Portable lamp",
        "category_id": "601234",
        "skus": [
            {
                "seller_sku": "LAMP-1",
                "price": "19.90",
                "currency": "MYR",
                "inventory_by_warehouse": {"warehouse-my": 1},
            }
        ],
        "images": [],
    }


def test_business_routes_require_session_and_report_blocked_capabilities(
    api_client: tuple[TestClient, object],
) -> None:
    client, _test_app = api_client
    path = f"/api/shops/{_SHOP_ID}/products/capabilities"
    denied = client.get(path)
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

    _csrf, _cookie = _login(client)
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == {
        "platform_configured": False,
        "master_key_configured": False,
        "image_upload_enabled": False,
        "product_submission_enabled": False,
        "blockers": [
            "BLOCKED_LIVE_CREDENTIALS",
            "BLOCKED_MASTER_KEY",
            "BLOCKED_UNVERIFIED_IMAGE_UPLOAD_ENDPOINT",
            "BLOCKED_UNVERIFIED_LIVE_PRODUCT_VALIDATION",
        ],
    }
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["x-request-id"]


def test_session_cookie_is_httponly_and_only_digests_are_persisted(
    api_client: tuple[TestClient, object],
) -> None:
    client, test_app = api_client
    csrf_token, session_token = _login(client)
    cookie_header = client.post(
        "/api/session", json={"bootstrap_secret": _ADMIN_SECRET}
    ).headers["set-cookie"]
    assert "HttpOnly" in cookie_header
    assert "SameSite=strict" in cookie_header

    async def stored_session() -> AdminSession:
        async with test_app.state.db_session_factory() as session:
            records = tuple(await session.scalars(select(AdminSession).order_by(AdminSession.created_at)))
            return records[0]

    record = asyncio.run(stored_session())
    assert record.session_digest != session_token
    assert record.csrf_digest != csrf_token
    assert session_token not in repr(record)
    assert csrf_token not in repr(record)


def test_csrf_and_strict_payload_validation_fail_closed_without_echoing_secrets(
    api_client: tuple[TestClient, object],
) -> None:
    client, _test_app = api_client
    _csrf, _cookie = _login(client)
    path = f"/api/shops/{_SHOP_ID}/products/drafts"
    payload = _draft_payload()
    missing_csrf = client.post(path, json=payload)
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "CSRF_REJECTED"

    secret_value = "must-never-appear-in-response"
    invalid = client.post(
        "/api/session",
        json={"bootstrap_secret": _ADMIN_SECRET, "access_token": secret_value},
    )
    assert invalid.status_code == 422
    serialized = invalid.text.lower()
    assert secret_value not in serialized
    assert "access_token" not in serialized
    assert "traceback" not in serialized


def test_product_draft_contract_maps_only_normalized_business_fields(
    api_client: tuple[TestClient, object],
) -> None:
    client, test_app = api_client
    csrf, _cookie = _login(client)

    class ContractProductService:
        async def save_draft(self, _session: object, _context: object, product: object) -> object:
            draft = ProductDraft(
                id="22222222-2222-4222-8222-222222222222",
                shop_binding_id=_SHOP_ID,
                source_kind="MANUAL",
                title=product.title,
                normalized_payload=normalized_product_to_payload(product),
                payload_hash="a" * 64,
                status=ProductDraftStatus.DRAFT.value,
                human_confirmed=False,
            )
            return SimpleNamespace(draft=draft, created=True)

    async def access_override() -> ShopAccessContext:
        return _context()

    test_app.dependency_overrides[shop_access_context] = access_override
    test_app.dependency_overrides[commerce_runtime] = lambda: SimpleNamespace(
        product_service=ContractProductService()
    )
    try:
        response = client.post(
            f"/api/shops/{_SHOP_ID}/products/drafts",
            headers={"X-CSRF-Token": csrf},
            json=_draft_payload(),
        )
    finally:
        test_app.dependency_overrides.clear()
    assert response.status_code == 201
    assert response.json()["product"]["skus"][0]["seller_sku"] == "LAMP-1"
    serialized = response.text.lower()
    assert "access_token" not in serialized
    assert "shop_cipher" not in serialized


def test_order_contract_uses_only_pii_minimized_domain_facts(
    api_client: tuple[TestClient, object],
) -> None:
    client, test_app = api_client
    _csrf, _cookie = _login(client)

    class ContractOrderService:
        async def fetch_page(self, *_args: object, **_kwargs: object) -> NormalizedOrderPage:
            return NormalizedOrderPage(
                orders=(
                    NormalizedOrder(
                        order_id="order-1",
                        status="AWAITING_SHIPMENT",
                        currency="MYR",
                        lines=(
                            NormalizedOrderLine(
                                line_id="line-1",
                                product_id="product-1",
                                sku_id="sku-1",
                                seller_sku="SELLER-SKU-1",
                                quantity=2,
                            ),
                        ),
                    ),
                ),
                next_page_token=None,
                total_count=1,
                request_id="request-1",
            )

    async def access_override() -> ShopAccessContext:
        return _context()

    test_app.dependency_overrides[shop_access_context] = access_override
    test_app.dependency_overrides[commerce_runtime] = lambda: SimpleNamespace(
        order_service=ContractOrderService()
    )
    try:
        response = client.get(f"/api/shops/{_SHOP_ID}/orders/remote")
    finally:
        test_app.dependency_overrides.clear()
    assert response.status_code == 200
    body = response.json()
    assert body["orders"][0]["order_id"] == "order-1"
    serialized = response.text.lower()
    for forbidden in (
        "buyer",
        "recipient",
        "address",
        "phone",
        "email",
        "access_token",
        "shop_cipher",
    ):
        assert forbidden not in serialized


def test_upstream_error_mapping_never_exposes_exception_content(
    api_client: tuple[TestClient, object],
) -> None:
    client, test_app = api_client
    _csrf, _cookie = _login(client)

    class FailingOrderService:
        async def fetch_page(self, *_args: object, **_kwargs: object) -> NormalizedOrderPage:
            raise OrderGatewayError(
                "buyer_email=person@example.com access_token=very-secret-upstream-token"
            )

    async def access_override() -> ShopAccessContext:
        return _context()

    test_app.dependency_overrides[shop_access_context] = access_override
    test_app.dependency_overrides[commerce_runtime] = lambda: SimpleNamespace(
        order_service=FailingOrderService()
    )
    try:
        response = client.get(f"/api/shops/{_SHOP_ID}/orders/remote")
    finally:
        test_app.dependency_overrides.clear()
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "TIKTOK_RESPONSE_INVALID"
    serialized = response.text.lower()
    assert "person@example.com" not in serialized
    assert "very-secret-upstream-token" not in serialized
    assert "traceback" not in serialized