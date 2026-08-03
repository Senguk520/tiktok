from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditLog, ShopBinding
from app.domain.enums import AuthorizationStatus
from app.integrations.translation import (
    AzureTranslator,
    AzureTranslatorConfig,
    TranslationConfigurationBlocked,
    TranslationRequest,
    TranslationRequestRejected,
    TranslationUpstreamError,
)
from app.main import create_app
from app.repositories.audit import AuditFactRejected, record_audit_fact, sanitize_audit_details
from app.use_cases.profit import (
    ProfitInputError,
    ProfitInputs,
    estimate_profit,
    profit_for_price,
)
from shared.safe_paths import PROJECT_ROOT

_ADMIN_SECRET = "value-tools-admin-secret-with-32-characters"
_SHOP_ID = "11111111-1111-4111-8111-111111111111"


def test_profit_uses_decimal_currency_rounding_and_explicit_margin() -> None:
    inputs = ProfitInputs(
        product_cost=Decimal("100"),
        source_currency="CNY",
        settlement_currency="MYR",
        exchange_rate=Decimal("0.65"),
        shipping_cost=Decimal("10"),
        other_fixed_cost=Decimal("5"),
        commission_rate=Decimal("0.10"),
        payment_fee_rate=Decimal("0.02"),
        target_margin_rate=Decimal("0.20"),
    )
    estimate = estimate_profit(inputs)
    assert estimate.converted_product_cost == Decimal("65.00")
    assert estimate.total_fixed_cost == Decimal("80.00")
    assert estimate.suggested_price == Decimal("117.65")
    assert estimate.commission_amount == Decimal("11.77")
    assert estimate.payment_fee_amount == Decimal("2.35")
    assert estimate.estimated_profit == Decimal("23.53")
    assert estimate.realized_margin_rate == Decimal("0.2000")
    assert estimate.realized_margin_percent == Decimal("20.00")

    at_price = profit_for_price(inputs, Decimal("100"))
    assert at_price.estimated_profit == Decimal("8.00")
    assert at_price.realized_margin_percent == Decimal("8.00")


@pytest.mark.parametrize(
    "changes",
    [
        {"source_currency": "MYR", "settlement_currency": "MYR", "exchange_rate": Decimal("2")},
        {"commission_rate": Decimal("0.8"), "target_margin_rate": Decimal("0.3")},
        {"product_cost": Decimal("NaN")},
        {"settlement_currency": "BTC"},
    ],
)
def test_profit_fails_closed_for_ambiguous_or_invalid_inputs(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "product_cost": Decimal("10"),
        "source_currency": "CNY",
        "settlement_currency": "MYR",
        "exchange_rate": Decimal("0.65"),
        "shipping_cost": Decimal("1"),
        "other_fixed_cost": Decimal("0"),
        "commission_rate": Decimal("0.1"),
        "payment_fee_rate": Decimal("0.02"),
        "target_margin_rate": Decimal("0.2"),
    }
    values.update(changes)
    with pytest.raises(ProfitInputError):
        ProfitInputs(**values)  # type: ignore[arg-type]


def test_translation_contract_limits_languages_counts_and_characters() -> None:
    request = TranslationRequest(("你好",), "zh-hans", "MS")
    assert request.source_language == "zh-Hans"
    assert request.target_language == "ms"
    with pytest.raises(TranslationRequestRejected):
        TranslationRequest(("text",), "fr", "en")
    with pytest.raises(TranslationRequestRejected):
        TranslationRequest(("x" * 5001,), "en", "ms")
    with pytest.raises(TranslationRequestRejected):
        TranslationRequest(("same",), "en", "en")


def test_azure_configuration_requires_backend_secret_and_approved_https_host() -> None:
    with pytest.raises(TranslationConfigurationBlocked):
        AzureTranslatorConfig.from_env({})
    with pytest.raises(TranslationConfigurationBlocked):
        AzureTranslatorConfig(
            subscription_key="provider-secret",
            region="southeastasia",
            endpoint="https://example.com",
        )
    config = AzureTranslatorConfig(
        subscription_key="provider-secret",
        region="southeastasia",
    )
    assert "provider-secret" not in repr(config)


@pytest.mark.asyncio
async def test_azure_translator_uses_verified_v3_contract_without_caching() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "api.cognitive.microsofttranslator.com"
        assert request.url.path == "/translate"
        assert request.url.params["api-version"] == "3.0"
        assert request.url.params["from"] == "zh-Hans"
        assert request.url.params["to"] == "en"
        assert request.url.params["textType"] == "plain"
        assert request.headers["Ocp-Apim-Subscription-Key"] == "provider-secret"
        assert request.headers["Ocp-Apim-Subscription-Region"] == "southeastasia"
        assert json.loads(request.content) == [{"Text": "你好"}]
        return httpx.Response(
            200,
            json=[{"translations": [{"text": "Hello", "to": "en"}]}],
            headers={"X-RequestId": "azure-request-1"},
        )

    provider = AzureTranslator(
        AzureTranslatorConfig(
            subscription_key="provider-secret",
            region="southeastasia",
        ),
        transport=httpx.MockTransport(handler),
    )
    request = TranslationRequest(("你好",), "zh-Hans", "en")
    first = await provider.translate(request)
    second = await provider.translate(request)
    assert first.texts == ("Hello",)
    assert first.request_id == "azure-request-1"
    assert second.texts == first.texts
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_azure_translator_returns_stable_failure_without_upstream_body() -> None:
    secret_body = "access_token=do-not-leak buyer@example.com"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=secret_body)

    provider = AzureTranslator(
        AzureTranslatorConfig(
            subscription_key="provider-secret",
            region="southeastasia",
        ),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TranslationUpstreamError) as caught:
        await provider.translate(TranslationRequest(("hello",), "en", "ms"))
    assert caught.value.code == "azure_translator_unavailable"
    assert secret_body not in str(caught.value)


def test_audit_allowlist_rejects_secrets_pii_and_local_paths() -> None:
    assert sanitize_audit_details(
        {"code": "schedule_completed", "item_count": 2}
    ) == {"code": "schedule_completed", "item_count": 2}
    for details in (
        {"access_token": "secret"},
        {"code": "person@example.com"},
        {"reason": "C:\\Users\\operator\\secret.txt"},
        {"reason": "https://example.com/?token=secret"},
    ):
        with pytest.raises(AuditFactRejected):
            sanitize_audit_details(details)


@pytest.fixture
def value_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, object]]:
    database_path = PROJECT_ROOT / "data" / f"test-value-api-{uuid4()}.sqlite3"
    monkeypatch.setenv("CORE_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ADMIN_BOOTSTRAP_SECRET", _ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "false")
    for key in (
        "APP_MASTER_KEY",
        "TIKTOK_APP_KEY",
        "TIKTOK_APP_SECRET",
        "AZURE_TRANSLATOR_KEY",
        "AZURE_TRANSLATOR_REGION",
    ):
        monkeypatch.delenv(key, raising=False)
    app = create_app()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            async def seed() -> None:
                async with app.state.db_session_factory.begin() as session:
                    session.add(
                        ShopBinding(
                            id=_SHOP_ID,
                            open_id="owner-1",
                            shop_id="shop-1",
                            region="MY",
                            authorization_status=AuthorizationStatus.ACTIVE.value,
                        )
                    )

            asyncio.run(seed())
            yield client, app
    finally:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{database_path}{suffix}")
            if path.exists():
                path.unlink()


def _login(client: TestClient) -> str:
    response = client.post("/api/session", json={"bootstrap_secret": _ADMIN_SECRET})
    assert response.status_code == 201
    return str(response.json()["csrf_token"])


def test_tools_api_reports_real_configuration_and_never_persists_translation_text(
    value_api: tuple[TestClient, object],
) -> None:
    client, app = value_api
    csrf = _login(client)
    capabilities = client.get(f"/api/shops/{_SHOP_ID}/tools/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["translation_configured"] is False
    assert capabilities.json()["translation_cache_enabled"] is False

    source_text = "private text buyer@example.com"
    response = client.post(
        f"/api/shops/{_SHOP_ID}/tools/translate",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "translation-key-0001",
        },
        json={
            "texts": [source_text],
            "source_language": "en",
            "target_language": "ms",
        },
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BLOCKED_AZURE_TRANSLATOR_CONFIGURATION"
    assert source_text not in response.text

    async def stored_audits() -> tuple[AuditLog, ...]:
        async with app.state.db_session_factory() as session:
            return tuple(await session.scalars(select(AuditLog)))

    records = asyncio.run(stored_audits())
    assert records[-1].event_type == "translation.blocked"
    assert source_text not in json.dumps(records[-1].redacted_details)


def test_profit_and_webhook_routes_use_csrf_and_fail_closed_audit(
    value_api: tuple[TestClient, object],
) -> None:
    client, app = value_api
    csrf = _login(client)
    profit = client.post(
        f"/api/shops/{_SHOP_ID}/tools/profit",
        headers={"X-CSRF-Token": csrf},
        json={
            "product_cost": "100",
            "source_currency": "CNY",
            "settlement_currency": "MYR",
            "exchange_rate": "0.65",
            "shipping_cost": "10",
            "other_fixed_cost": "5",
            "commission_rate": "0.10",
            "payment_fee_rate": "0.02",
            "target_margin_rate": "0.20",
        },
    )
    assert profit.status_code == 200
    assert profit.json()["suggested_price"] == "117.65"

    invalid_profit = client.post(
        f"/api/shops/{_SHOP_ID}/tools/profit",
        headers={"X-CSRF-Token": csrf},
        json={
            "product_cost": "100",
            "source_currency": "CNY",
            "settlement_currency": "MYR",
            "exchange_rate": "0.65",
            "commission_rate": "0.80",
            "payment_fee_rate": "0.10",
            "target_margin_rate": "0.20",
        },
    )
    assert invalid_profit.status_code == 422
    assert invalid_profit.json()["error"]["code"] == "PROFIT_REQUEST_INVALID"

    webhook_secret = "Authorization=secret-token buyer@example.com"
    webhook = client.post(
        "/api/webhooks/tiktok",
        headers={"Authorization": "secret-signature"},
        content=webhook_secret,
    )
    assert webhook.status_code == 503
    assert webhook.json()["error"]["code"] == "WEBHOOK_SIGNATURE_CONTRACT_UNVERIFIED"
    assert webhook_secret not in webhook.text

    async def webhook_audit() -> AuditLog:
        async with app.state.db_session_factory() as session:
            record = await session.scalar(
                select(AuditLog).where(AuditLog.event_type == "webhook.rejected")
            )
            assert record is not None
            return record

    record = asyncio.run(webhook_audit())
    serialized = json.dumps(record.redacted_details)
    assert "secret-token" not in serialized
    assert "buyer@example.com" not in serialized


@pytest.mark.asyncio
async def test_recorded_audit_contains_only_allowed_business_facts() -> None:
    # The repository is also exercised without HTTP to prove that callers
    # cannot bypass the allowlist with a nested secret field.
    class RejectingSession:
        pass

    with pytest.raises(AuditFactRejected):
        await record_audit_fact(  # type: ignore[arg-type]
            RejectingSession(),
            event_type="translation.failed",
            outcome="FAILED",
            details={"code": ["safe", "person@example.com"]},
        )