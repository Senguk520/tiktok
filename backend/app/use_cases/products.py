"""Product application service: drafts -> images -> quota -> one gateway."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import session_scope
from app.db.models import IdempotentOperation, ProductDraft, ProductImageAsset, QuotaSnapshotModel
from app.domain.enums import ListingMode, OperationKind, ProductDraftStatus, WriteState
from app.domain.product import NormalizedProduct
from app.domain.product_payload import normalized_product_from_payload
from app.integrations.tiktok.endpoints import ENDPOINTS, Endpoint
from app.integrations.tiktok.errors import ErrorCategory
from app.integrations.tiktok.products import (
    ProductPage,
    ProductSubmission,
    UploadedProductImage,
    product_create_chain_endpoint_keys,
)
from app.repositories.catalog import (
    DraftConflict,
    complete_image_upload,
    confirm_product_draft,
    get_owned_draft,
    product_links_for_seller_skus,
    record_product_submission,
    record_quota_snapshot,
    register_image_upload,
    release_listing_quota,
    reserve_listing_quota,
    save_product_draft,
)
from app.repositories.idempotency import IdempotencyRequest, register_operation
from app.use_cases.commerce_context import ShopAccessContext
from shared.safe_paths import validate_image_bytes


class ProductGateway(Protocol):
    async def upload_image(
        self,
        context: ShopAccessContext,
        *,
        content: bytes,
        filename: str,
        content_type: str,
        use_case: str,
    ) -> UploadedProductImage: ...

    async def create(
        self,
        context: ShopAccessContext,
        product: NormalizedProduct,
        *,
        reconcile: Any = None,
    ) -> ProductSubmission: ...

    async def search(
        self,
        context: ShopAccessContext,
        *,
        page_size: int = 20,
        page_token: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> ProductPage: ...

    async def get(
        self,
        context: ShopAccessContext,
        product_id: str,
    ) -> Mapping[str, Any]: ...


class ProductSubmissionBlocked(PermissionError):
    pass


class ProductSubmissionInProgress(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DraftResult:
    draft: ProductDraft
    created: bool


@dataclass(frozen=True, slots=True)
class ImageUploadResult:
    asset: ProductImageAsset
    draft: ProductDraft
    replayed: bool


@dataclass(frozen=True, slots=True)
class DraftSubmissionResult:
    submission: ProductSubmission
    operation_id: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class PreparedDraftSubmission:
    draft_id: str
    operation_id: str
    product: NormalizedProduct
    quota_snapshot_id: str | None
    reconciliation_required: bool = False
    known_product_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DraftSubmissionPreparation:
    prepared: PreparedDraftSubmission | None = None
    replayed: DraftSubmissionResult | None = None

    def __post_init__(self) -> None:
        if (self.prepared is None) == (self.replayed is None):
            raise ValueError("submission preparation must be prepared or replayed")


@dataclass(frozen=True, slots=True)
class ProductCapabilityEvidence:
    """Endpoint-registry evidence used to fail closed before platform calls."""

    registry: Mapping[str, Endpoint] = field(default_factory=lambda: ENDPOINTS)

    def endpoint_blockers(self, *endpoint_keys: str) -> tuple[str, ...]:
        blockers: list[str] = []
        for endpoint_key in endpoint_keys:
            selected = self.registry.get(endpoint_key)
            if selected is None:
                blockers.append(f"endpoint_not_registered:{endpoint_key}")
                continue
            if not selected.verified:
                blockers.append(f"endpoint_unverified:{endpoint_key}")
            if not selected.enabled:
                blockers.append(f"endpoint_disabled:{endpoint_key}")
        return tuple(blockers)

    def require_endpoints(self, *endpoint_keys: str) -> None:
        blockers = self.endpoint_blockers(*endpoint_keys)
        if blockers:
            raise ProductSubmissionBlocked(
                "TikTok product capability is blocked by endpoint registry evidence: "
                + ",".join(blockers)
            )

    def require_image_upload(self) -> None:
        self.require_endpoints("product.image.upload")

    def require_submission(self, mode: ListingMode) -> None:
        self.require_endpoints(*product_create_chain_endpoint_keys(mode))


def _failure_category(exc: BaseException) -> ErrorCategory | None:
    failure = getattr(exc, "failure", None)
    category = getattr(failure, "category", None)
    return category if isinstance(category, ErrorCategory) else None


def _submission_from_operation(
    *,
    context: ShopAccessContext,
    result_reference: str,
    request_id: str | None,
) -> ProductSubmission:
    return ProductSubmission(
        mode=context.listing_mode,
        product_id=result_reference,
        request_id=request_id,
    )


def _seller_skus(value: Mapping[str, Any]) -> frozenset[str]:
    raw_skus = value.get("skus")
    if not isinstance(raw_skus, list):
        return frozenset()
    return frozenset(
        item["seller_sku"].strip()
        for item in raw_skus
        if isinstance(item, Mapping)
        and isinstance(item.get("seller_sku"), str)
        and item["seller_sku"].strip()
    )


def _product_id(value: Mapping[str, Any], mode: ListingMode) -> str | None:
    keys = (
        ("product_id", "id")
        if mode is ListingMode.LOCAL_REPLICATION
        else ("global_product_id", "product_id", "id")
    )
    for key in keys:
        identifier = value.get(key)
        if identifier is not None and str(identifier).strip():
            return str(identifier).strip()
    return None


class ProductService:
    def __init__(
        self,
        gateway: ProductGateway,
        *,
        capabilities: ProductCapabilityEvidence | None = None,
    ) -> None:
        self._gateway = gateway
        self._capabilities = capabilities or ProductCapabilityEvidence()

    async def save_draft(
        self,
        session: AsyncSession,
        context: ShopAccessContext,
        product: NormalizedProduct,
        *,
        source_kind: str = "MANUAL",
        source_result_id: str | None = None,
        field_sources: Mapping[str, Any] | None = None,
    ) -> DraftResult:
        context.require_active()
        draft, created = await save_product_draft(
            session,
            shop_binding_id=context.shop_binding_id,
            product=product,
            source_kind=source_kind,
            source_result_id=source_result_id,
            field_sources=field_sources,
        )
        return DraftResult(draft=draft, created=created)

    async def confirm_draft(
        self,
        session: AsyncSession,
        context: ShopAccessContext,
        draft_id: str,
    ) -> ProductDraft:
        context.require_active()
        return await confirm_product_draft(
            session,
            shop_binding_id=context.shop_binding_id,
            draft_id=draft_id,
        )

    async def upload_draft_image(
        self,
        session: AsyncSession,
        context: ShopAccessContext,
        *,
        draft_id: str,
        source_ref: str,
        content: bytes,
        filename: str,
        content_type: str,
        use_case: str = "MAIN_IMAGE",
    ) -> ImageUploadResult:
        context.require_active()
        self._capabilities.require_image_upload()
        safe_filename = validate_image_bytes(
            filename,
            content_type=content_type,
            content=content,
        )
        draft = await get_owned_draft(
            session,
            shop_binding_id=context.shop_binding_id,
            draft_id=draft_id,
        )
        if draft.status not in {ProductDraftStatus.DRAFT.value, ProductDraftStatus.READY.value}:
            raise DraftConflict("images can only be attached to an editable draft")
        product = normalized_product_from_payload(draft.normalized_payload)
        if source_ref not in {image.source_url for image in product.images}:
            raise DraftConflict("image source reference is not part of the draft")
        asset, created = await register_image_upload(
            session,
            draft=draft,
            source_ref=source_ref,
            content=content,
            content_type=content_type,
        )
        if not created:
            if asset.upload_state == WriteState.ACTIVE.value and asset.tiktok_image_id:
                return ImageUploadResult(asset=asset, draft=draft, replayed=True)
            raise ProductSubmissionInProgress("the image upload is already registered")
        asset.upload_state = WriteState.SUBMITTED.value
        try:
            uploaded = await self._gateway.upload_image(
                context,
                content=content,
                filename=safe_filename,
                content_type=content_type,
                use_case=use_case,
            )
        except BaseException as exc:
            asset.upload_state = WriteState.FAILED.value
            category = _failure_category(exc)
            asset.last_error_code = category.value if category else type(exc).__name__
            asset.last_error_redacted = "TikTok image upload failed"
            raise
        updated_draft = await complete_image_upload(
            session,
            shop_binding_id=context.shop_binding_id,
            draft_id=draft.id,
            asset_id=asset.id,
            source_ref=source_ref,
            image_id=uploaded.image_id,
            request_id=uploaded.request_id,
        )
        return ImageUploadResult(asset=asset, draft=updated_draft, replayed=False)

    async def confirm_quota(
        self,
        session: AsyncSession,
        context: ShopAccessContext,
        *,
        listing_limit: int | None,
        locally_submitted_count: int,
        confirmed_at: datetime,
        expires_at: datetime,
        stage: str | None = None,
        confirmed_by_session_id: str | None = None,
    ) -> QuotaSnapshotModel:
        context.require_active()
        if context.region.strip().upper() != "MY":
            raise ProductSubmissionBlocked(
                "automatic quota policy is only verified for Malaysia; other regions require a dedicated policy"
            )
        return await record_quota_snapshot(
            session,
            shop_binding_id=context.shop_binding_id,
            region=context.region,
            listing_limit=listing_limit,
            locally_submitted_count=locally_submitted_count,
            confirmed_at=confirmed_at,
            expires_at=expires_at,
            stage=stage,
            confirmed_by_session_id=confirmed_by_session_id,
        )

    async def prepare_draft_submission(
        self,
        session: AsyncSession,
        context: ShopAccessContext,
        *,
        draft_id: str,
        idempotency_key: str,
    ) -> DraftSubmissionPreparation:
        mode = context.require_listing_write()
        self._capabilities.require_submission(mode)
        draft = await get_owned_draft(
            session,
            shop_binding_id=context.shop_binding_id,
            draft_id=draft_id,
        )
        if draft.status not in {
            ProductDraftStatus.READY.value,
            ProductDraftStatus.SUBMITTED.value,
        } or not draft.human_confirmed:
            raise ProductSubmissionBlocked("product draft requires explicit human confirmation")
        product = normalized_product_from_payload(draft.normalized_payload)
        if not product.ready_for_platform_submission:
            raise ProductSubmissionBlocked("all draft images must be uploaded before submission")
        known_links = await product_links_for_seller_skus(
            session,
            shop_binding_id=context.shop_binding_id,
            seller_skus=tuple(sku.seller_sku for sku in product.skus),
        )
        known_product_ids = tuple(
            dict.fromkeys(
                identifier
                for link in known_links
                for identifier in (
                    (
                        link.local_product_by_region.get(context.region)
                        if mode is ListingMode.LOCAL_REPLICATION
                        else link.global_product_id
                    ),
                )
                if identifier
            )
        )
        operation, created = await register_operation(
            session,
            IdempotencyRequest(
                shop_binding_id=context.shop_binding_id,
                operation=OperationKind.CREATE.value,
                business_key=draft.id,
                payload_hash=draft.payload_hash,
                idempotency_key=idempotency_key,
            ),
        )
        if not created:
            if operation.result_reference and operation.state in {
                WriteState.AUDITING.value,
                WriteState.ACTIVE.value,
            }:
                return DraftSubmissionPreparation(
                    replayed=DraftSubmissionResult(
                        submission=_submission_from_operation(
                            context=context,
                            result_reference=operation.result_reference,
                            request_id=operation.platform_request_id,
                        ),
                        operation_id=operation.id,
                        replayed=True,
                    )
                )
            if operation.state != WriteState.SUBMITTED.value:
                raise ProductSubmissionInProgress(
                    f"product submission already exists in state {operation.state}"
                )
            return DraftSubmissionPreparation(
                prepared=PreparedDraftSubmission(
                    draft_id=draft.id,
                    operation_id=operation.id,
                    product=product,
                    quota_snapshot_id=None,
                    reconciliation_required=True,
                    known_product_ids=known_product_ids,
                )
            )

        reconciliation_required = (
            draft.status == ProductDraftStatus.SUBMITTED.value or bool(known_product_ids)
        )
        operation.state = WriteState.QUEUED.value
        quota_snapshot_id: str | None = None
        if mode is ListingMode.LOCAL_REPLICATION and not reconciliation_required:
            quota_snapshot_id = await reserve_listing_quota(
                session,
                shop_binding_id=context.shop_binding_id,
                region=context.region,
                now=datetime.now(UTC),
            )
        operation.state = WriteState.SUBMITTED.value
        return DraftSubmissionPreparation(
            prepared=PreparedDraftSubmission(
                draft_id=draft.id,
                operation_id=operation.id,
                product=product,
                quota_snapshot_id=quota_snapshot_id,
                reconciliation_required=reconciliation_required,
                known_product_ids=known_product_ids,
            )
        )

    async def reconcile_draft_submission(
        self,
        context: ShopAccessContext,
        prepared: PreparedDraftSubmission,
        *,
        max_pages: int = 3,
    ) -> ProductSubmission | None:
        if not 1 <= max_pages <= 10:
            raise ValueError("product reconciliation page limit is invalid")
        expected_skus = frozenset(sku.seller_sku for sku in prepared.product.skus)
        candidate_ids = set(prepared.known_product_ids)
        complete_search = bool(candidate_ids)
        if not candidate_ids:
            page_token: str | None = None
            seen_tokens: set[str] = set()
            seen_items = 0
            expected_total: int | None = None
            for _ in range(max_pages):
                page = await self._gateway.search(
                    context,
                    page_size=100,
                    page_token=page_token,
                )
                if page.mode is not context.listing_mode:
                    return None
                if page.total_count is not None:
                    if page.total_count < 0:
                        return None
                    if expected_total is None:
                        expected_total = page.total_count
                    elif expected_total != page.total_count:
                        return None
                seen_items += len(page.items)
                for item in page.items:
                    if _seller_skus(item) != expected_skus:
                        continue
                    identifier = _product_id(item, context.listing_mode)
                    if identifier is not None:
                        candidate_ids.add(identifier)
                if page.next_page_token is None:
                    complete_search = expected_total is None or seen_items >= expected_total
                    break
                if page.next_page_token in seen_tokens:
                    break
                seen_tokens.add(page.next_page_token)
                page_token = page.next_page_token
        if not complete_search or len(candidate_ids) != 1:
            return None
        product_id = next(iter(candidate_ids))
        details = await self._gateway.get(context, product_id)
        if (
            _product_id(details, context.listing_mode) != product_id
            or _seller_skus(details) != expected_skus
        ):
            return None
        return ProductSubmission(
            mode=context.listing_mode,
            product_id=product_id,
            request_id=None,
            raw_status=(
                str(details["status"])
                if details.get("status") is not None
                else None
            ),
        )

    async def execute_draft_submission(
        self,
        context: ShopAccessContext,
        prepared: PreparedDraftSubmission,
    ) -> ProductSubmission:
        return await self._gateway.create(context, prepared.product)

    async def require_manual_reconciliation(
        self,
        session: AsyncSession,
        prepared: PreparedDraftSubmission,
        *,
        reason: str,
    ) -> None:
        operation = await session.get(IdempotentOperation, prepared.operation_id)
        if operation is None or operation.state != WriteState.SUBMITTED.value:
            raise ProductSubmissionInProgress(
                "product submission state changed before manual review"
            )
        operation.state = WriteState.MANUAL_REVIEW.value
        operation.manual_review_reason = reason

    async def fail_draft_submission(
        self,
        session: AsyncSession,
        prepared: PreparedDraftSubmission,
        exc: BaseException,
    ) -> None:
        operation = await session.get(IdempotentOperation, prepared.operation_id)
        if operation is None or operation.state != WriteState.SUBMITTED.value:
            raise ProductSubmissionInProgress("product submission state changed before failure recording")
        category = _failure_category(exc)
        recovery_required = category not in {
            ErrorCategory.AUTHORIZATION,
            ErrorCategory.SCOPE,
            ErrorCategory.VALIDATION,
        }
        operation.state = (
            WriteState.SUBMITTED.value if recovery_required else WriteState.FAILED.value
        )
        operation.manual_review_reason = (
            "TikTok create result is uncertain; reconcile by exact seller SKU before retry"
            if recovery_required
            else "TikTok create request was rejected before a confirmed result"
        )
        if prepared.quota_snapshot_id is not None and not recovery_required:
            await release_listing_quota(session, snapshot_id=prepared.quota_snapshot_id)

    async def complete_draft_submission(
        self,
        session: AsyncSession,
        context: ShopAccessContext,
        prepared: PreparedDraftSubmission,
        submission: ProductSubmission,
    ) -> DraftSubmissionResult:
        operation = await session.get(IdempotentOperation, prepared.operation_id)
        draft = await get_owned_draft(
            session,
            shop_binding_id=context.shop_binding_id,
            draft_id=prepared.draft_id,
        )
        if operation is None or operation.state != WriteState.SUBMITTED.value:
            raise ProductSubmissionInProgress("product submission state changed before completion")
        operation.state = WriteState.AUDITING.value
        operation.platform_request_id = submission.request_id
        operation.result_reference = submission.product_id
        await record_product_submission(
            session,
            draft=draft,
            mode=context.listing_mode,
            region=context.region,
            product_id=submission.product_id,
            request_id=submission.request_id,
        )
        return DraftSubmissionResult(
            submission=submission,
            operation_id=operation.id,
            replayed=False,
        )

    async def submit_draft(
        self,
        factory: async_sessionmaker[AsyncSession],
        context: ShopAccessContext,
        *,
        draft_id: str,
        idempotency_key: str,
    ) -> DraftSubmissionResult:
        """Commit durable intent before dispatch and complete in a fresh transaction."""

        async with session_scope(factory) as session:
            preparation = await self.prepare_draft_submission(
                session,
                context,
                draft_id=draft_id,
                idempotency_key=idempotency_key,
            )
        if preparation.replayed is not None:
            return preparation.replayed
        prepared = preparation.prepared
        if prepared is None:  # pragma: no cover - guarded by the frozen value contract
            raise RuntimeError("submission preparation is incomplete")

        if prepared.reconciliation_required:
            submission = await self.reconcile_draft_submission(context, prepared)
            if submission is None:
                async with session_scope(factory) as session:
                    await self.require_manual_reconciliation(
                        session,
                        prepared,
                        reason=(
                            "TikTok create cannot be uniquely reconciled from a complete same-mode search"
                        ),
                    )
                raise ProductSubmissionInProgress(
                    "product submission requires manual review before any further create"
                )
        else:
            try:
                submission = await self.execute_draft_submission(context, prepared)
            except BaseException as exc:
                async with session_scope(factory) as session:
                    await self.fail_draft_submission(session, prepared, exc)
                raise

        async with session_scope(factory) as session:
            return await self.complete_draft_submission(
                session,
                context,
                prepared,
                submission,
            )
