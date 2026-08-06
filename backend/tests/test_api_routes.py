from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.dependencies import commerce_runtime, shop_access_context
from app.db.models import (
    AdminSession,
    AuditLog,
    ListingModeEvidence,
    ProductDraft,
    QuotaSnapshotModel,
    ScopeSnapshot,
    ShopBinding,
)
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
        "listing_mode": "UNKNOWN",
        "image_upload_enabled": False,
        "product_submission_enabled": False,
        "image_upload_blockers": [
            "BLOCKED_LIVE_CREDENTIALS",
            "BLOCKED_MASTER_KEY",
            "BLOCKED_SHOP_BINDING",
        ],
        "product_submission_blockers": [
            "BLOCKED_LIVE_CREDENTIALS",
            "BLOCKED_MASTER_KEY",
            "BLOCKED_SHOP_BINDING",
        ],
        "blockers": [
            "BLOCKED_LIVE_CREDENTIALS",
            "BLOCKED_MASTER_KEY",
            "BLOCKED_SHOP_BINDING",
        ],
    }
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["x-request-id"]


def test_shop_registry_exposes_only_safe_capability_facts_and_blocks_inactive_selection(
    api_client: tuple[TestClient, object],
) -> None:
    client, test_app = api_client
    denied = client.get("/api/shops")
    assert denied.status_code == 401
    _csrf, _cookie = _login(client)
    now = datetime.now(UTC)

    async def seed() -> None:
        async with test_app.state.db_session_factory.begin() as session:
            session.add_all(
                (
                    ShopBinding(
                        id=_SHOP_ID,
                        open_id="private-owner-active",
                        shop_id="platform-shop-active",
                        shop_code="MY-ACTIVE",
                        region="MY",
                        shop_status="ACTIVE",
                        kyc_status="VERIFIED",
                        listing_mode=ListingMode.LOCAL_REPLICATION.value,
                        authorization_status=AuthorizationStatus.ACTIVE.value,
                    ),
                    ShopBinding(
                        id="33333333-3333-4333-8333-333333333333",
                        open_id="private-owner-disabled",
                        shop_id="platform-shop-disabled",
                        shop_code="MY-DISABLED",
                        region="MY",
                        shop_status="INACTIVE",
                        kyc_status="VERIFIED",
                        listing_mode=ListingMode.UNKNOWN.value,
                        authorization_status=AuthorizationStatus.DEAUTHORIZED.value,
                    ),
                )
            )
            await session.flush()
            session.add_all(
                (
                    ScopeSnapshot(
                        shop_binding_id=_SHOP_ID,
                        granted_scopes=[
                            Scope.PRODUCT_BASIC.value,
                            Scope.ORDER_INFO.value,
                        ],
                        missing_scopes=[Scope.PRODUCT_WRITE.value],
                        captured_at=now,
                        access_expires_at=now + timedelta(hours=1),
                    ),
                    QuotaSnapshotModel(
                        shop_binding_id=_SHOP_ID,
                        region="MY",
                        stage="BEGINNER",
                        listing_limit=1000,
                        locally_submitted_count=1,
                        confirmed_at=now,
                        expires_at=now + timedelta(hours=1),
                    ),
                )
            )

    asyncio.run(seed())
    response = client.get("/api/shops")
    assert response.status_code == 200
    body = response.json()
    active, inactive = body["items"]
    assert active["id"] == _SHOP_ID
    assert active["selectable"] is True
    assert active["product_read_enabled"] is True
    assert active["order_read_enabled"] is True
    assert active["product_write_preconditions_met"] is False
    assert active["product_write_blockers"] == ["BLOCKED_PRODUCT_WRITE_SCOPE"]
    assert inactive["selectable"] is False
    assert "BLOCKED_SHOP_AUTHORIZATION" in inactive["product_read_blockers"]
    serialized = response.text.lower()
    for forbidden in (
        "private-owner-active",
        "private-owner-disabled",
        "open_id",
        "access_token",
        "refresh_token",
        "shop_cipher",
        "ciphertext",
    ):
        assert forbidden not in serialized


def test_listing_mode_confirmation_is_strict_durable_and_conflicts_to_unknown(
    api_client: tuple[TestClient, object],
) -> None:
    client, test_app = api_client
    csrf, _cookie = _login(client)
    now = datetime.now(UTC)

    async def seed() -> None:
        async with test_app.state.db_session_factory.begin() as session:
            binding = ShopBinding(
                id=_SHOP_ID,
                open_id="listing-mode-owner",
                shop_id="listing-mode-target",
                region="MY",
                shop_status="ACTIVE",
                kyc_status="VERIFIED",
                listing_mode=ListingMode.UNKNOWN.value,
                authorization_status=AuthorizationStatus.ACTIVE.value,
            )
            session.add(binding)
            await session.flush()
            session.add(
                ScopeSnapshot(
                    shop_binding_id=_SHOP_ID,
                    granted_scopes=[
                        Scope.PRODUCT_BASIC.value,
                        Scope.PRODUCT_WRITE.value,
                    ],
                    missing_scopes=[],
                    captured_at=now,
                    access_expires_at=now + timedelta(hours=1),
                )
            )

    asyncio.run(seed())
    path = f"/api/shops/{_SHOP_ID}/listing-mode-confirmations"
    wrong_target = client.post(
        path,
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "listing-mode-wrong-target-key",
        },
        json={
            "target_shop_id": "different-shop",
            "mode": "LOCAL_REPLICATION",
            "local_read_verified": True,
            "global_read_verified": False,
        },
    )
    assert wrong_target.status_code == 403
    assert wrong_target.json()["error"]["code"] == "COMMERCE_ACCESS_BLOCKED"

    confirmed = client.post(
        path,
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "listing-mode-confirm-key",
        },
        json={
            "target_shop_id": "listing-mode-target",
            "mode": "LOCAL_REPLICATION",
            "local_read_verified": True,
            "global_read_verified": False,
        },
    )
    assert confirmed.status_code == 201
    assert confirmed.json()["mode"] == "LOCAL_REPLICATION"
    assert confirmed.json()["writable"] is True
    evidence_id = confirmed.json()["recorded_evidence_id"]
    assert evidence_id

    replayed = client.post(
        path,
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "listing-mode-confirm-key",
        },
        json={
            "target_shop_id": "listing-mode-target",
            "mode": "LOCAL_REPLICATION",
            "local_read_verified": True,
            "global_read_verified": False,
        },
    )
    assert replayed.status_code == 200
    assert replayed.json()["recorded_evidence_id"] == evidence_id
    assert replayed.json()["mode"] == "LOCAL_REPLICATION"

    replay_conflict = client.post(
        path,
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "listing-mode-confirm-key",
        },
        json={
            "target_shop_id": "listing-mode-target",
            "mode": "GLOBAL_LEGACY",
            "local_read_verified": False,
            "global_read_verified": True,
        },
    )
    assert replay_conflict.status_code == 409

    conflict = client.post(
        path,
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "listing-mode-conflict-key",
        },
        json={
            "target_shop_id": "listing-mode-target",
            "mode": "GLOBAL_LEGACY",
            "local_read_verified": False,
            "global_read_verified": True,
        },
    )
    assert conflict.status_code == 201
    assert conflict.json()["mode"] == "UNKNOWN"
    assert conflict.json()["writable"] is False
    assert conflict.json()["blockers"] == ["conflicting listing-mode evidence"]
    current = client.get(f"/api/shops/{_SHOP_ID}/listing-mode")
    assert current.status_code == 200
    assert current.json()["mode"] == "UNKNOWN"

    async def persisted_facts() -> tuple[ShopBinding | None, tuple[ListingModeEvidence, ...], tuple[AuditLog, ...]]:
        async with test_app.state.db_session_factory() as session:
            binding = await session.get(ShopBinding, _SHOP_ID)
            evidence = tuple(
                await session.scalars(
                    select(ListingModeEvidence).order_by(ListingModeEvidence.recorded_at)
                )
            )
            audits = tuple(
                await session.scalars(select(AuditLog).order_by(AuditLog.created_at))
            )
            return binding, evidence, audits

    binding, evidence, audits = asyncio.run(persisted_facts())
    assert binding is not None and binding.listing_mode == ListingMode.UNKNOWN.value
    assert len(evidence) == 2
    assert evidence[0].read_only_endpoint == "/product/202502/products/search"
    assert evidence[1].read_only_endpoint == "/product/202312/global_products/search"
    assert evidence[1].conflict is True
    assert [audit.outcome for audit in audits] == ["SUCCESS", "BLOCKED"]
    assert all(audit.event_type == "listing_mode.confirmed" for audit in audits)


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


def test_browser_submission_route_delegates_transaction_boundaries_to_service(
    api_client: tuple[TestClient, object],
) -> None:
    client, test_app = api_client
    csrf, _cookie = _login(client)
    observed: dict[str, object] = {}

    class ContractProductService:
        async def submit_draft(
            self,
            factory: object,
            context: ShopAccessContext,
            *,
            draft_id: str,
            idempotency_key: str,
        ) -> object:
            observed.update(
                factory=factory,
                context=context,
                draft_id=draft_id,
                idempotency_key=idempotency_key,
            )
            return SimpleNamespace(
                submission=SimpleNamespace(
                    mode=ListingMode.LOCAL_REPLICATION,
                    product_id="product-1",
                    request_id="request-1",
                ),
                operation_id="operation-1",
                replayed=False,
            )

    async def access_override() -> ShopAccessContext:
        return _context()

    test_app.dependency_overrides[shop_access_context] = access_override
    test_app.dependency_overrides[commerce_runtime] = lambda: SimpleNamespace(
        product_service=ContractProductService()
    )
    draft_id = "22222222-2222-4222-8222-222222222222"
    try:
        response = client.post(
            f"/api/shops/{_SHOP_ID}/products/drafts/{draft_id}/submit",
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "browser-submit-key",
            },
        )
    finally:
        test_app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["product_id"] == "product-1"
    assert observed["factory"] is test_app.state.db_session_factory
    assert observed["draft_id"] == draft_id
    assert observed["idempotency_key"] == "browser-submit-key"


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