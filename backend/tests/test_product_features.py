from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.db.base import DatabaseSettings, create_engine_and_session_factory, session_scope
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
from app.integrations.tiktok.products import ProductPage, ProductSubmission, UploadedProductImage
from app.repositories.catalog import ListingQuotaBlocked
from app.use_cases.commerce_context import CommerceAccessBlocked, ShopAccessContext
from app.use_cases.products import (
    ProductCapabilityEvidence,
    ProductService,
    ProductSubmissionBlocked,
    ProductSubmissionInProgress,
)
from migrations.core import migrate_engine

PNG = b"\x89PNG\r\n\x1a\n" + b"safe-test-image"
VERIFIED_REGISTRY = dict(ENDPOINTS)
VERIFIED_REGISTRY["product.image.upload"] = replace(
    ENDPOINTS["product.image.upload"],
    enabled=True,
    verified=True,
)
VERIFIED_CAPABILITIES = ProductCapabilityEvidence(registry=VERIFIED_REGISTRY)


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

    async def search(
        self,
        context: ShopAccessContext,
        *,
        page_size: int = 20,
        page_token: str | None = None,
        filters: object | None = None,
    ) -> ProductPage:
        assert filters is None
        return ProductPage(
            mode=context.listing_mode,
            items=(),
            next_page_token=None,
            total_count=0,
            request_id="search-request",
        )

    async def get(
        self,
        _context: ShopAccessContext,
        _product_id: str,
    ) -> dict[str, object]:
        raise AssertionError("unexpected product detail request")


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


def platform_product_intent() -> NormalizedProduct:
    product = product_intent()
    return replace(
        product,
        images=(replace(product.images[0], local_image_id="tos-image-id"),),
    )


async def seed_ready_submission(
    session_factory: Any,
    service: ProductService,
    *,
    mode: ListingMode,
    identity: str,
) -> tuple[ShopAccessContext, str]:
    now = datetime.now(UTC)
    async with session_scope(session_factory) as session:
        binding = ShopBinding(
            open_id=f"owner-{identity}",
            shop_id=f"shop-{identity}",
            region="MY",
            listing_mode=mode.value,
            authorization_status=AuthorizationStatus.ACTIVE.value,
        )
        session.add(binding)
        await session.flush()
        access = context(binding, mode)
        if mode is ListingMode.LOCAL_REPLICATION:
            await service.confirm_quota(
                session,
                access,
                listing_limit=10,
                locally_submitted_count=0,
                confirmed_at=now,
                expires_at=now + timedelta(hours=4),
            )
        draft = (await service.save_draft(session, access, platform_product_intent())).draft
        await service.confirm_draft(session, access, draft.id)
        return access, draft.id


class RecoveryProductGateway(FakeProductGateway):
    def __init__(self, session_factory: Any, *, scenario: str = "success") -> None:
        super().__init__()
        self.session_factory = session_factory
        self.scenario = scenario
        self.prepare_was_committed = False
        self.search_tokens: list[str | None] = []
        self.get_calls: list[tuple[ListingMode, str]] = []

    @staticmethod
    def _item(mode: ListingMode, product_id: str, *, seller_sku: str = "LAMP-BLACK") -> dict[str, object]:
        id_key = "product_id" if mode is ListingMode.LOCAL_REPLICATION else "global_product_id"
        return {id_key: product_id, "skus": [{"seller_sku": seller_sku}], "status": "PENDING"}

    async def create(
        self,
        context: ShopAccessContext,
        product: NormalizedProduct,
        *,
        reconcile: Any = None,
    ) -> ProductSubmission:
        async with self.session_factory() as session:
            operation = await session.scalar(select(IdempotentOperation))
            self.prepare_was_committed = (
                operation is not None and operation.state == WriteState.SUBMITTED.value
            )
        return await super().create(context, product, reconcile=reconcile)

    async def search(
        self,
        context: ShopAccessContext,
        *,
        page_size: int = 20,
        page_token: str | None = None,
        filters: object | None = None,
    ) -> ProductPage:
        assert page_size == 100
        assert filters is None
        self.search_tokens.append(page_token)
        product_id = (
            "local-product-1"
            if context.listing_mode is ListingMode.LOCAL_REPLICATION
            else "global-product-1"
        )
        if self.scenario == "zero":
            items: tuple[dict[str, object], ...] = ()
            next_token = None
            total_count = 0
        elif self.scenario == "multiple":
            items = (
                self._item(context.listing_mode, product_id),
                self._item(context.listing_mode, f"{product_id}-duplicate"),
            )
            next_token = None
            total_count = 2
        elif self.scenario == "incomplete":
            items = (self._item(context.listing_mode, product_id),)
            next_token = f"next-{len(self.search_tokens)}"
            total_count = 4
        else:
            items = (self._item(context.listing_mode, product_id),)
            next_token = None
            total_count = 1
        return ProductPage(
            mode=context.listing_mode,
            items=items,
            next_page_token=next_token,
            total_count=total_count,
            request_id="search-request",
        )

    async def get(
        self,
        context: ShopAccessContext,
        product_id: str,
    ) -> dict[str, object]:
        self.get_calls.append((context.listing_mode, product_id))
        seller_sku = "WRONG-SKU" if self.scenario == "detail_mismatch" else "LAMP-BLACK"
        return self._item(context.listing_mode, product_id, seller_sku=seller_sku)


class FailOnceCompletionProductService(ProductService):
    def __init__(self, gateway: RecoveryProductGateway) -> None:
        super().__init__(gateway, capabilities=VERIFIED_CAPABILITIES)
        self.fail_next_completion = True

    async def complete_draft_submission(
        self,
        session: Any,
        context: ShopAccessContext,
        prepared: Any,
        submission: ProductSubmission,
    ) -> Any:
        if self.fail_next_completion:
            self.fail_next_completion = False
            raise RuntimeError("simulated local completion failure")
        return await super().complete_draft_submission(
            session,
            context,
            prepared,
            submission,
        )


def test_normalized_product_payload_is_stable_and_strict() -> None:
    product = product_intent()
    payload = normalized_product_to_payload(product)
    assert normalized_product_from_payload(payload) == product
    payload["skus"][0]["inventory_by_warehouse"]["warehouse-my"] = "5"
    with pytest.raises(ProductPayloadError, match="integer"):
        normalized_product_from_payload(payload)


def test_image_endpoint_is_registered_but_disabled_without_verified_evidence() -> None:
    endpoint = ENDPOINTS["product.image.upload"]
    assert endpoint.path == "/product/202309/images/upload"
    assert endpoint.scope is Scope.PRODUCT_WRITE
    assert not endpoint.enabled
    assert endpoint.official_anomaly
    assert not endpoint.automatic_retry_allowed


@pytest.mark.asyncio
async def test_unverified_product_calls_fail_before_gateway() -> None:
    engine, session_factory = await factory()
    gateway = FakeProductGateway()
    service = ProductService(gateway)
    try:
        async with session_factory() as session:
            binding = ShopBinding(
                open_id="owner-blocked",
                shop_id="shop-blocked",
                region="MY",
                listing_mode=ListingMode.LOCAL_REPLICATION.value,
                authorization_status=AuthorizationStatus.ACTIVE.value,
            )
            session.add(binding)
            await session.flush()
            access = context(binding, ListingMode.LOCAL_REPLICATION)
            with pytest.raises(ProductSubmissionBlocked, match="endpoint registry"):
                await service.upload_draft_image(
                    session,
                    access,
                    draft_id="not-read",
                    source_ref="collector-image:main",
                    content=PNG,
                    filename="main.png",
                    content_type="image/png",
                )
            with pytest.raises(ProductSubmissionBlocked, match="endpoint registry"):
                await service.submit_draft(
                    session_factory,
                    access,
                    draft_id="not-read",
                    idempotency_key="not-persisted",
                )
            assert gateway.upload_calls == 0
            assert gateway.create_modes == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_local_draft_image_quota_and_submission_are_one_durable_chain() -> None:
    engine, session_factory = await factory()
    gateway = FakeProductGateway()
    service = ProductService(gateway, capabilities=VERIFIED_CAPABILITIES)
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
            await session.commit()

            submitted = await service.submit_draft(
                session_factory,
                access,
                draft_id=saved.draft.id,
                idempotency_key="create-lamp-1",
            )
            replayed = await service.submit_draft(
                session_factory,
                access,
                draft_id=saved.draft.id,
                idempotency_key="create-lamp-1",
            )
            assert submitted.submission.product_id == "local-product-1"
            assert replayed.replayed
            assert gateway.create_modes == [ListingMode.LOCAL_REPLICATION]

            submitted_draft_id = saved.draft.id
            session.expire_all()
            quota = await session.scalar(select(QuotaSnapshotModel))
            draft = await session.get(ProductDraft, submitted_draft_id)
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
    service = ProductService(gateway, capabilities=VERIFIED_CAPABILITIES)
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
            await session.commit()
            result = await service.submit_draft(
                session_factory,
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
    service = ProductService(gateway, capabilities=VERIFIED_CAPABILITIES)
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
                    session_factory,
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
            await session.commit()
            with pytest.raises(ListingQuotaBlocked, match="BLOCK_UNKNOWN"):
                await service.submit_draft(
                    session_factory,
                    local,
                    draft_id=draft.id,
                    idempotency_key="quota-blocked",
                )
            assert gateway.create_modes == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [ListingMode.LOCAL_REPLICATION, ListingMode.GLOBAL_LEGACY],
)
async def test_completion_failure_recovers_without_a_second_create(mode: ListingMode) -> None:
    engine, session_factory = await factory()
    gateway = RecoveryProductGateway(session_factory)
    service = FailOnceCompletionProductService(gateway)
    try:
        access, draft_id = await seed_ready_submission(
            session_factory,
            service,
            mode=mode,
            identity=f"recover-{mode.value.lower()}",
        )
        with pytest.raises(RuntimeError, match="local completion failure"):
            await service.submit_draft(
                session_factory,
                access,
                draft_id=draft_id,
                idempotency_key=f"recover-{mode.value.lower()}",
            )
        assert gateway.prepare_was_committed
        assert gateway.create_modes == [mode]
        async with session_factory() as session:
            operation = await session.scalar(select(IdempotentOperation))
            assert operation is not None and operation.state == WriteState.SUBMITTED.value
            assert await session.scalar(select(ProductLink)) is None

        recovered = await service.submit_draft(
            session_factory,
            access,
            draft_id=draft_id,
            idempotency_key=f"recover-{mode.value.lower()}",
        )
        replayed = await service.submit_draft(
            session_factory,
            access,
            draft_id=draft_id,
            idempotency_key=f"recover-{mode.value.lower()}",
        )
        assert recovered.submission.mode is mode
        assert recovered.submission.product_id.endswith("product-1")
        assert replayed.replayed
        assert gateway.create_modes == [mode]
        assert gateway.search_tokens == [None]
        assert gateway.get_calls == [(mode, recovered.submission.product_id)]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["zero", "multiple", "incomplete", "detail_mismatch"])
async def test_non_unique_reconciliation_enters_manual_review_and_never_recreates(
    scenario: str,
) -> None:
    engine, session_factory = await factory()
    gateway = RecoveryProductGateway(session_factory)
    service = FailOnceCompletionProductService(gateway)
    try:
        access, draft_id = await seed_ready_submission(
            session_factory,
            service,
            mode=ListingMode.LOCAL_REPLICATION,
            identity=f"manual-{scenario}",
        )
        key = f"manual-review-{scenario}"
        with pytest.raises(RuntimeError, match="local completion failure"):
            await service.submit_draft(
                session_factory,
                access,
                draft_id=draft_id,
                idempotency_key=key,
            )
        gateway.scenario = scenario
        with pytest.raises(ProductSubmissionInProgress, match="manual review"):
            await service.submit_draft(
                session_factory,
                access,
                draft_id=draft_id,
                idempotency_key=key,
            )
        upstream_counts = (
            len(gateway.create_modes),
            len(gateway.search_tokens),
            len(gateway.get_calls),
        )
        with pytest.raises(ProductSubmissionInProgress, match="MANUAL_REVIEW"):
            await service.submit_draft(
                session_factory,
                access,
                draft_id=draft_id,
                idempotency_key=key,
            )
        assert upstream_counts == (
            len(gateway.create_modes),
            len(gateway.search_tokens),
            len(gateway.get_calls),
        )
        assert gateway.create_modes == [ListingMode.LOCAL_REPLICATION]
        async with session_factory() as session:
            operation = await session.scalar(select(IdempotentOperation))
            assert operation is not None and operation.state == WriteState.MANUAL_REVIEW.value
            assert "same-mode search" in (operation.manual_review_reason or "")
    finally:
        await engine.dispose()
