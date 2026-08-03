"""Product application service: drafts -> images -> quota -> one gateway."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IdempotentOperation, ProductDraft, ProductImageAsset, QuotaSnapshotModel
from app.domain.enums import ListingMode, OperationKind, ProductDraftStatus, WriteState
from app.domain.product import NormalizedProduct
from app.domain.product_payload import normalized_product_from_payload
from app.integrations.tiktok.errors import ErrorCategory
from app.integrations.tiktok.products import ProductSubmission, UploadedProductImage
from app.repositories.catalog import (
    DraftConflict,
    complete_image_upload,
    confirm_product_draft,
    get_owned_draft,
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


@dataclass(frozen=True, slots=True)
class DraftSubmissionPreparation:
    prepared: PreparedDraftSubmission | None = None
    replayed: DraftSubmissionResult | None = None

    def __post_init__(self) -> None:
        if (self.prepared is None) == (self.replayed is None):
            raise ValueError("submission preparation must be prepared or replayed")


@dataclass(frozen=True, slots=True)
class ProductCapabilityEvidence:
    """Runtime evidence gates for platform calls whose exact contracts are not yet verified."""

    image_upload_verified: bool = False
    live_submission_validation_verified: bool = False

    def require_image_upload(self) -> None:
        if not self.image_upload_verified:
            raise ProductSubmissionBlocked(
                "TikTok product image upload is blocked until its exact official endpoint is verified"
            )

    def require_submission(self) -> None:
        if not self.live_submission_validation_verified:
            raise ProductSubmissionBlocked(
                "TikTok product submission is blocked until live category and attribute validation is verified"
            )


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
        self._capabilities.require_submission()
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
            raise ProductSubmissionInProgress(
                f"product submission already exists in state {operation.state}"
            )
        if draft.status != ProductDraftStatus.READY.value:
            operation.state = WriteState.MANUAL_REVIEW.value
            operation.manual_review_reason = (
                "submitted draft has no matching durable platform result"
            )
            raise ProductSubmissionInProgress(
                "submitted draft requires reconciliation before another platform call"
            )
        operation.state = WriteState.QUEUED.value
        quota_snapshot_id: str | None = None
        if mode is ListingMode.LOCAL_REPLICATION:
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
            )
        )

    async def execute_draft_submission(
        self,
        context: ShopAccessContext,
        prepared: PreparedDraftSubmission,
    ) -> ProductSubmission:
        return await self._gateway.create(context, prepared.product)

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
        ambiguous = category is ErrorCategory.AMBIGUOUS_WRITE
        operation.state = WriteState.MANUAL_REVIEW.value if ambiguous else WriteState.FAILED.value
        operation.manual_review_reason = (
            "TikTok create result is ambiguous; reconcile by seller SKU before retry"
            if ambiguous
            else "TikTok create request failed before a confirmed result"
        )
        if prepared.quota_snapshot_id is not None and not ambiguous:
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
        session: AsyncSession,
        context: ShopAccessContext,
        *,
        draft_id: str,
        idempotency_key: str,
    ) -> DraftSubmissionResult:
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
        try:
            submission = await self.execute_draft_submission(context, prepared)
        except BaseException as exc:
            await self.fail_draft_submission(session, prepared, exc)
            raise
        return await self.complete_draft_submission(
            session,
            context,
            prepared,
            submission,
        )
