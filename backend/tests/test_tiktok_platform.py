from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.db.base import DatabaseSettings, create_engine_and_session_factory
from app.db.models import EncryptedCredential
from app.domain.enums import ListingMode, Scope
from app.domain.scopes import ScopeSet
from app.integrations.tiktok.client import should_retry
from app.integrations.tiktok.endpoints import ENDPOINTS, endpoint
from app.integrations.tiktok.errors import ErrorCategory, classify_failure
from app.integrations.tiktok.oauth import OAuthClient, OAuthConfig, TokenSet
from app.integrations.tiktok.signing import sign_request, signature_base
from app.use_cases.authorization import (
    AuthorizationError,
    AuthorizedShop,
    begin_authorization,
    bind_authorization,
    consume_authorization_state,
)
from app.use_cases.listing_mode import (
    ListingModeBlocked,
    ListingModeFacts,
    assert_listing_write_allowed,
    determine_listing_mode,
)
from migrations.core import migrate_engine
from shared.security import KeyRing, MasterKey, decrypt_text


def test_endpoint_registry_keeps_independent_versions_and_anomaly_disabled() -> None:
    assert ENDPOINTS["local.create"].version == "202309"
    assert ENDPOINTS["local.full_edit"].version == "202509"
    assert ENDPOINTS["local.search"].version == "202502"
    assert ENDPOINTS["orders.search"].path == "/order/202309/orders/search"
    assert ENDPOINTS["orders.detail"].path == "/order/202507/orders"
    assert ENDPOINTS["global.search"].version == "202312"
    anomaly = ENDPOINTS["global.partial_edit_anomaly"]
    assert not anomaly.enabled and anomaly.official_anomaly
    with pytest.raises(PermissionError):
        endpoint("global.partial_edit_anomaly")


def test_signing_vector_sorts_query_and_excludes_legacy_secrets() -> None:
    query = {
        "timestamp": 1690000000,
        "shop_cipher": "ROW_x",
        "access_token": "must-not-be-signed",
        "sign": "old",
        "app_key": "123456",
    }
    assert signature_base("/product/202309/products/search", query) == (
        b"/product/202309/products/search"
        b"app_key123456shop_cipherROW_xtimestamp1690000000"
    )
    assert sign_request("test_secret", "/product/202309/products/search", query) == (
        "1d760e8a9eb6beacd22c1ba963783651f25f928cec2f8eeb03e9547ce409dfd3"
    )
    first = sign_request(
        "secret",
        "/product/202309/products",
        {"app_key": "key", "timestamp": 1690000000},
        body=b"first",
        content_type="multipart/form-data; boundary=abc",
    )
    second = sign_request(
        "secret",
        "/product/202309/products",
        {"app_key": "key", "timestamp": 1690000000},
        body=b"second",
        content_type="multipart/form-data; boundary=xyz",
    )
    assert first == second


def test_error_classification_and_retry_policy_are_conservative() -> None:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    limited = classify_failure(
        http_status=200,
        business_code=36009002,
        retry_after="7",
        now=now,
    )
    assert limited.category is ErrorCategory.RATE_LIMITED
    assert limited.retry_at == now + timedelta(seconds=7)
    unavailable = classify_failure(http_status=503)
    assert unavailable.category is ErrorCategory.SERVICE_UNAVAILABLE
    assert unavailable.category is not limited.category
    assert should_retry(
        endpoint("local.search"),
        unavailable,
        attempt=1,
        max_attempts=3,
        idempotency_registered=False,
        reconciliation_available=False,
    )
    assert not should_retry(
        endpoint("local.create"),
        limited,
        attempt=1,
        max_attempts=3,
        idempotency_registered=True,
        reconciliation_available=False,
    )
    assert should_retry(
        endpoint("local.create"),
        limited,
        attempt=1,
        max_attempts=3,
        idempotency_registered=True,
        reconciliation_available=True,
    )


def test_listing_mode_conflict_and_missing_evidence_fail_closed() -> None:
    local_scopes = ScopeSet.parse(
        [Scope.PRODUCT_BASIC, Scope.PRODUCT_WRITE, Scope.PRODUCT_DELETE]
    )
    unknown = determine_listing_mode(ListingModeFacts(scopes=local_scopes))
    assert unknown.mode is ListingMode.UNKNOWN
    with pytest.raises(ListingModeBlocked):
        assert_listing_write_allowed(unknown)
    conflict = determine_listing_mode(
        ListingModeFacts(
            migration_completed=True,
            operator_confirmed_mode=ListingMode.GLOBAL_LEGACY,
            local_read_verified=True,
            scopes=local_scopes,
        )
    )
    assert conflict.mode is ListingMode.UNKNOWN
    local = determine_listing_mode(
        ListingModeFacts(
            migration_completed=True,
            local_read_verified=True,
            scopes=local_scopes,
        )
    )
    assert assert_listing_write_allowed(local) is ListingMode.LOCAL_REPLICATION
    with pytest.raises(ListingModeBlocked):
        assert_listing_write_allowed(local, expected_mode=ListingMode.GLOBAL_LEGACY)


def test_oauth_uses_tiktok_authorized_code_grant() -> None:
    config = OAuthConfig(service_id="service", app_key="key", app_secret="secret")
    client = OAuthClient(config)
    assert client.authorized_code_query("one-time-code")["grant_type"] == "authorized_code"
    assert client.refresh_query("refresh")["grant_type"] == "refresh_token"
    url = config.authorization_url("state-value")
    query = parse_qs(urlsplit(url).query)
    assert query == {"service_id": ["service"], "state": ["state-value"]}


@pytest.mark.asyncio
async def test_oauth_state_is_single_use_and_credentials_are_encrypted() -> None:
    settings = DatabaseSettings(url="sqlite+aiosqlite:///:memory:", path=None)
    engine, factory = create_engine_and_session_factory(settings)
    await migrate_engine(engine)
    now = datetime.now(UTC)
    key = MasterKey("v1", b"k" * 32)
    try:
        async with factory() as session:
            config = OAuthConfig(service_id="service", app_key="key", app_secret="secret")
            start = await begin_authorization(session, config, now=now)
            state = parse_qs(urlsplit(start.url).query)["state"][0]
            await consume_authorization_state(session, state, now=now + timedelta(seconds=1))
            with pytest.raises(AuthorizationError):
                await consume_authorization_state(session, state, now=now + timedelta(seconds=2))

            tokens = TokenSet(
                access_token="access-plaintext",
                refresh_token="refresh-plaintext",
                open_id="open-1",
                user_type=0,
                granted_scopes=ScopeSet.parse(
                    [Scope.AUTHORIZATION_INFO, Scope.PRODUCT_BASIC, Scope.PRODUCT_WRITE]
                ),
                access_expires_at=now + timedelta(days=7),
                refresh_expires_at=now + timedelta(days=30),
            )
            binding = await bind_authorization(
                session,
                tokens=tokens,
                shops=(AuthorizedShop("shop-1", "cipher-plaintext", "MY"),),
                key=key,
                expected_scopes=[Scope.PRODUCT_BASIC, Scope.PRODUCT_WRITE],
            )
            assert binding.listing_mode == ListingMode.UNKNOWN.value
            credentials = tuple(await session.scalars(select(EncryptedCredential)))
            assert len(credentials) == 3
            assert all("plaintext" not in item.ciphertext for item in credentials)
            access = next(item for item in credentials if item.credential_kind == "access_token")
            assert decrypt_text(
                access.ciphertext,
                KeyRing.from_current(key),
                aad=access.aad_context,
            ) == "access-plaintext"
            await session.commit()
    finally:
        await engine.dispose()