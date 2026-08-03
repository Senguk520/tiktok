from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.db.base import DatabaseSettings, create_engine_and_session_factory
from app.db.models import (
    IdempotentOperation,
    ProductDraft,
    ProductImageAsset,
    ProductLink,
    QuotaSnapshotModel,
    ShopBinding,
)
from app.domain.enums import (
    AuthorizationStatus,
    ListingMode,
    ProductDraftStatus,
    Scope,
    WriteState,
)
from app.domain.product import NormalizedImage, NormalizedProduct, NormalizedSku
from app.domain.product_payload import (
    ProductPayloadError,
    normalized_product_from_payload,
    normalized_product_to_payload,
)
from app.domain.scopes import ScopeSet
from app.integrations.tiktok.endpoints import ENDPOINTS
from app.integrations.tiktok.products import ProductSubmission, UploadedProductImage
from app.repositories.catalog import ListingQuotaBlocked
from app.use_cases.commerce_context import CommerceAccessBlocked, ShopAccessContext
from app.use_cases.products import ProductService
from migrations.core import migrate_engine

PNG = b"\x89PNG\r\n\x1a\n" + b"safe-test-image"


class FakeProductGateway:
    def __init__(self) -> None:
        self.upload_calls = 0
        self.create_modes: list[ListingMode] = []

    async def upload_image(
        self,
        context: ShopAccessContext,
        *,
        content: bytes,
        filename: str,
        content_type: str,
        use_case: str,
    ) -> UploadedProductImage:
        self.upload_calls += 1
        assert content == PNG
        assert filename == "main.png"
        assert content_type == "image/png"
        assert use_case == "MAIN_IMAGE"
        return UploadedProductImage("tos-image-id", "upload-request")

    async def create(
        self,
        context: ShopAccessContext,
        product: NormalizedProduct,
        *,
        reconcile: Any = None,
    ) -> ProductSubmission:
        assert product.ready_for_platform_submission
        self.create_modes.append(context.listing_mode)
        return ProductSubmission(
            mode=context.listing_mode,
            product_id=(
                "local-product-1"
                if context.listing_mode is ListingMode.LOCAL_REPLICATION
                else "global-product-1"
            ),
            request_id="create-request",
            raw_status="PENDING",
        )


def product_intent() -> NormalizedProduct:
    return NormalizedProduct(
        title="Portable lamp",
        description="Rechargeable lamp",
        category_id="601234",
        skus=(
            NormalizedSku(
                seller_sku="LAMP-BLACK",
                price=Decimal("19.90"),
                currency="MYR",
                inventory_by_warehouse={"warehouse-my": 5},
                attributes={"color": "black"},
            ),
        ),
        images=(NormalizedImage(source_url="collector-image:main", role="MAIN"),),
        attributes={"brand": "no-brand"},
    )


def context(binding: ShopBinding, mode: ListingMode) -> ShopAccessContext:
    scopes = {
        Scope.PRODUCT_BASIC,
        Scope.PRODUCT_WRITE,
        Scope.PRODUCT_DELETE,
    }
    if mode is ListingMode.GLOBAL_LEGACY:
        scopes |= {
            Scope.GLOBAL_PRODUCT_INFO,
            Scope.GLOBAL_PRODUCT_WRITE,
            Scope.GLOBAL_PRODUCT_DELETE,
        }
    return ShopAccessContext(
        shop_binding_id=binding.id,
        shop_id=binding.shop_id,
        region=binding.region,
        listing_mode=mode,
        authorization_status=AuthorizationStatus.ACTIVE,
        scopes=ScopeSet(frozenset(scopes)),
        access_token="secret-access",
        shop_cipher="secret-cipher",
    )


async def factory():
    engine, session_factory = create_engine_and_session_factory(
        DatabaseSettings(url="sqlite+aiosqlite:///:memory:", path=None)
    )
    await migrate_engine(engine)
    return engine, session_factory


def test_normalized_product_payload_is_stable_and_strict() -> None:
    product = product_intent()
    payload = normalized_product_to_payload(product)
    assert normalized_product_from_payload(payload) == product
    payload["skus"][0]["inventory_by_warehouse"]["warehouse-my"] = "5"
    with pytest.raises(ProductPayloadError, match="integer"):
        normalized_product_from_payload(payload)


def test_image_endpoint_is_registered_without_automatic_retry() -> None:
    endpoint = ENDPOINTS["product.image.upload"]
    assert endpoint.path == "/product/202309/images/upload"
    assert endpoint.scope is Scope.PRODUCT_WRITE
    assert not endpoint.automatic_retry_allowed


@pytest.mark.asyncio
async def test_local_draft_image_quota_and_submission_are_one_durable_chain() -> None:
    engine, session_factory = await factory()
    gateway = FakeProductGateway()
    service = ProductService(gateway)
    now = datetime.now(UTC)
    try:
        async with session_factory() as session:
            binding = ShopBinding(
                open_id="owner-local",
                shop_id="shop-local",
                region="MY",
                listing_mode=ListingMode.LOCAL_REPLICATION.value,
                authorization_status=AuthorizationStatus.ACTIVE.value,
            )
            session.add(binding)
            await session.flush()
            access = context(binding, ListingMode.LOCAL_REPLICATION)
            await service.confirm_quota(
                session,
                access,
                listing_limit=10,
                locally_submitted_count=0,
                confirmed_at=now,
                expires_at=now + timedelta(hours=4),
                stage="BEGINNER",
            )
            saved = await service.save_draft(session, access, product_intent())
            assert saved.created and saved.draft.status == ProductDraftStatus.DRAFT.value
            confirmed = await service.confirm_draft(session, access, saved.draft.id)
            assert confirmed.status == ProductDraftStatus.READY.value

            uploaded = await service.upload_draft_image(
                session,
                access,
                draft_id=saved.draft.id,
                source_ref="collector-image:main",
                content=PNG,
                filename="main.png",
                content_type="image/png",
            )
            assert not uploaded.replayed
            assert uploaded.asset.tiktok_image_id == "tos-image-id"
            assert "secret" not in repr(access)

            submitted = await service.submit_draft(
                session,
                access,
                draft_id=saved.draft.id,
                idempotency_key="create-lamp-1",
            )
            replayed = await service.submit_draft(
                session,
                access,
                draft_id=saved.draft.id,
                idempotency_key="create-lamp-1",
            )
            assert submitted.submission.product_id == "local-product-1"
            assert replayed.replayed
            assert gateway.create_modes == [ListingMode.LOCAL_REPLICATION]

            quota = await session.scalar(select(QuotaSnapshotModel))
            draft = await session.get(ProductDraft, saved.draft.id)
            link = await session.scalar(select(ProductLink))
            operation = await session.scalar(select(IdempotentOperation))
            asset = await session.scalar(select(ProductImageAsset))
            assert quota is not None and quota.locally_submitted_count == 1
            assert draft is not None and draft.status == ProductDraftStatus.SUBMITTED.value
            assert link is not None and link.local_product_by_region == {"MY": "local-product-1"}
            assert operation is not None and operation.state == WriteState.AUDITING.value
            assert asset is not None and not hasattr(asset, "content")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_global_mode_never_falls_back_to_local_or_consumes_local_quota() -> None:
    engine, session_factory = await factory()
    gateway = FakeProductGateway()
    service = ProductService(gateway)
    try:
        async with session_factory() as session:
            binding = ShopBinding(
                open_id="owner-global",
                shop_id="shop-global",
                region="MY",
                listing_mode=ListingMode.GLOBAL_LEGACY.value,
                authorization_status=AuthorizationStatus.ACTIVE.value,
            )
            session.add(binding)
            await session.flush()
            access = context(binding, ListingMode.GLOBAL_LEGACY)
            draft = (await service.save_draft(session, access, product_intent())).draft
            await service.confirm_draft(session, access, draft.id)
            await service.upload_draft_image(
                session,
                access,
                draft_id=draft.id,
                source_ref="collector-image:main",
                content=PNG,
                filename="main.png",
                content_type="image/png",
            )
            result = await service.submit_draft(
                session,
                access,
                draft_id=draft.id,
                idempotency_key="create-global-lamp",
            )
            assert result.submission.product_id == "global-product-1"
            assert gateway.create_modes == [ListingMode.GLOBAL_LEGACY]
            assert await session.scalar(select(QuotaSnapshotModel)) is None
            link = await session.scalar(select(ProductLink))
            assert link is not None and link.global_product_id == "global-product-1"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_mode_and_unknown_quota_fail_before_gateway_call() -> None:
    engine, session_factory = await factory()
    gateway = FakeProductGateway()
    service = ProductService(gateway)
    try:
        async with session_factory() as session:
            binding = ShopBinding(
                open_id="owner-unknown",
                shop_id="shop-unknown",
                region="MY",
                listing_mode=ListingMode.UNKNOWN.value,
                authorization_status=AuthorizationStatus.ACTIVE.value,
            )
            session.add(binding)
            await session.flush()
            unknown = context(binding, ListingMode.UNKNOWN)
            with pytest.raises(CommerceAccessBlocked, match="not verified"):
                await service.submit_draft(
                    session,
                    unknown,
                    draft_id="missing",
                    idempotency_key="blocked",
                )

            local = context(binding, ListingMode.LOCAL_REPLICATION)
            draft = (await service.save_draft(session, local, product_intent())).draft
            await service.confirm_draft(session, local, draft.id)
            await service.upload_draft_image(
                session,
                local,
                draft_id=draft.id,
                source_ref="collector-image:main",
                content=PNG,
                filename="main.png",
                content_type="image/png",
            )
            with pytest.raises(ListingQuotaBlocked, match="BLOCK_UNKNOWN"):
                await service.submit_draft(
                    session,
                    local,
                    draft_id=draft.id,
                    idempotency_key="quota-blocked",
                )
            assert gateway.create_modes == []
    finally:
        await engine.dispose()