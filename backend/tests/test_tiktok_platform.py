from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.db.base import DatabaseSettings, create_engine_and_session_factory
from app.db.models import EncryptedCredential, ListingModeEvidence, ScopeSnapshot
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
from app.use_cases.product_capabilities import evaluate_product_capabilities
from app.use_cases.products import ProductCapabilityEvidence
from migrations.core import migrate_engine
from shared.security import KeyRing, MasterKey, decrypt_text


def test_endpoint_registry_keeps_independent_versions_and_anomaly_disabled() -> None:
    assert ENDPOINTS["local.create"].version == "202309"
    assert ENDPOINTS["local.full_edit"].version == "202509"
    assert ENDPOINTS["local.search"].version == "202502"
    assert ENDPOINTS["orders.search"].path == "/order/202309/orders/search"
    assert ENDPOINTS["orders.detail"].path == "/order/202507/orders"
    assert ENDPOINTS["global.search"].version == "202312"
    assert ENDPOINTS["global.search"].path == "/product/202312/global_products/search"
    assert ENDPOINTS["global.full_edit"].version == "202309"
    assert ENDPOINTS["global.full_edit"].path == (
        "/product/202309/global_products/{global_product_id}"
    )
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


@pytest.mark.asyncio
async def test_product_capabilities_follow_registry_scopes_credentials_and_mode() -> None:
    settings = DatabaseSettings(url="sqlite+aiosqlite:///:memory:", path=None)
    engine, factory = create_engine_and_session_factory(settings)
    await migrate_engine(engine)
    now = datetime.now(UTC)
    key = MasterKey("v1", b"p" * 32)
    key_ring = KeyRing.from_current(key)
    registry = dict(ENDPOINTS)
    registry["product.image.upload"] = replace(
        ENDPOINTS["product.image.upload"],
        enabled=True,
        verified=True,
    )
    enabled_evidence = ProductCapabilityEvidence(registry=registry)
    try:
        async with factory() as session:
            tokens = TokenSet(
                access_token="access-token",
                refresh_token="refresh-token",
                open_id="capability-owner",
                user_type=0,
                granted_scopes=ScopeSet.parse(
                    [Scope.PRODUCT_BASIC, Scope.PRODUCT_WRITE]
                ),
                access_expires_at=now + timedelta(hours=2),
                refresh_expires_at=now + timedelta(days=30),
            )
            binding = await bind_authorization(
                session,
                tokens=tokens,
                shops=(
                    AuthorizedShop(
                        "capability-shop",
                        "shop-cipher",
                        "MY",
                        shop_status="ACTIVE",
                        kyc_status="VERIFIED",
                    ),
                ),
                key=key,
                expected_scopes=[Scope.PRODUCT_BASIC, Scope.PRODUCT_WRITE],
            )
            session.add(
                ListingModeEvidence(
                    shop_binding_id=binding.id,
                    evidence_source="OPERATOR_TARGET_ACCOUNT_CONFIRMATION",
                    observed_value=ListingMode.LOCAL_REPLICATION.value,
                    supports_local=True,
                    supports_global=False,
                    read_only_endpoint=ENDPOINTS["local.search"].path,
                    conflict=False,
                )
            )
            binding.listing_mode = ListingMode.LOCAL_REPLICATION.value
            await session.flush()

            default_blocked = await evaluate_product_capabilities(
                session,
                shop_binding_id=binding.id,
                platform_configured=True,
                key_ring=key_ring,
                endpoint_evidence=ProductCapabilityEvidence(),
                now=now,
            )
            assert default_blocked.listing_mode is ListingMode.LOCAL_REPLICATION
            assert "BLOCKED_ENDPOINT_UNVERIFIED:product.image.upload" in default_blocked.blockers
            assert "BLOCKED_ENDPOINT_DISABLED:product.image.upload" in default_blocked.blockers
            assert not default_blocked.image_upload_enabled
            assert not default_blocked.product_submission_enabled

            enabled = await evaluate_product_capabilities(
                session,
                shop_binding_id=binding.id,
                platform_configured=True,
                key_ring=key_ring,
                endpoint_evidence=enabled_evidence,
                now=now,
            )
            assert enabled.image_upload_enabled
            assert enabled.product_submission_enabled
            assert enabled.blockers == ()

            for field, blocked_value, restored_value, expected_blocker in (
                (
                    "authorization_status",
                    "DEAUTHORIZED",
                    "ACTIVE",
                    "BLOCKED_SHOP_AUTHORIZATION",
                ),
                ("shop_status", "INACTIVE", "ACTIVE", "BLOCKED_SHOP_STATUS"),
                ("kyc_status", "PENDING", "VERIFIED", "BLOCKED_KYC_STATUS"),
            ):
                setattr(binding, field, blocked_value)
                await session.flush()
                blocked = await evaluate_product_capabilities(
                    session,
                    shop_binding_id=binding.id,
                    platform_configured=True,
                    key_ring=key_ring,
                    endpoint_evidence=enabled_evidence,
                    now=now,
                )
                assert expected_blocker in blocked.blockers
                assert not blocked.image_upload_enabled
                assert not blocked.product_submission_enabled
                setattr(binding, field, restored_value)
                await session.flush()

            snapshot = await session.scalar(
                select(ScopeSnapshot)
                .where(ScopeSnapshot.shop_binding_id == binding.id)
                .order_by(ScopeSnapshot.captured_at.desc(), ScopeSnapshot.id.desc())
                .limit(1)
            )
            assert snapshot is not None
            snapshot.access_expires_at = now
            await session.flush()
            expired = await evaluate_product_capabilities(
                session,
                shop_binding_id=binding.id,
                platform_configured=True,
                key_ring=key_ring,
                endpoint_evidence=enabled_evidence,
                now=now,
            )
            assert "BLOCKED_ACCESS_TOKEN_EXPIRED" in expired.blockers
            assert not expired.image_upload_enabled
            assert not expired.product_submission_enabled
            snapshot.access_expires_at = now + timedelta(hours=2)
            await session.flush()

            session.add(
                ScopeSnapshot(
                    shop_binding_id=binding.id,
                    granted_scopes=[Scope.PRODUCT_BASIC.value],
                    missing_scopes=[Scope.PRODUCT_WRITE.value],
                    captured_at=now + timedelta(minutes=1),
                    access_expires_at=now + timedelta(hours=2),
                )
            )
            await session.flush()
            missing_scope = await evaluate_product_capabilities(
                session,
                shop_binding_id=binding.id,
                platform_configured=True,
                key_ring=key_ring,
                endpoint_evidence=enabled_evidence,
                now=now,
            )
            assert "BLOCKED_SCOPE:seller.product.write" in missing_scope.blockers
            assert not missing_scope.product_submission_enabled

            access = await session.scalar(
                select(EncryptedCredential).where(
                    EncryptedCredential.credential_kind == "access_token"
                )
            )
            assert access is not None
            access.active = False
            await session.flush()
            missing_credential = await evaluate_product_capabilities(
                session,
                shop_binding_id=binding.id,
                platform_configured=True,
                key_ring=key_ring,
                endpoint_evidence=enabled_evidence,
                now=now,
            )
            assert "BLOCKED_ACCESS_CREDENTIAL" in missing_credential.blockers
            assert not missing_credential.product_submission_enabled
    finally:
        await engine.dispose()
