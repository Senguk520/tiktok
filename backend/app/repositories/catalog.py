"""Persistence operations for product drafts, image assets, quota and links."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MarketProductState,
    ProductDraft,
    ProductImageAsset,
    ProductLink,
    QuotaSnapshotModel,
)
from app.domain.enums import ListingMode, ProductDraftStatus, WriteState
from app.domain.product import NormalizedProduct
from app.domain.product_payload import (
    bind_uploaded_image,
    normalized_product_from_payload,
    normalized_product_to_payload,
)
from app.domain.quota import QuotaDecision, QuotaSnapshot, decide_listing_quota
from app.repositories.idempotency import canonical_payload_hash


class DraftNotFound(LookupError):
    pass


class DraftConflict(ValueError):
    pass


class ListingQuotaBlocked(PermissionError):
    def __init__(self, decision: QuotaDecision) -> None:
        super().__init__(f"listing quota blocked: {decision.value}")
        self.decision = decision


def source_reference_hash(value: str) -> str:
    if not value.strip():
        raise ValueError("image source reference is required")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_sha256(value: bytes) -> str:
    if not value:
        raise ValueError("image content is empty")
    return hashlib.sha256(value).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def get_owned_draft(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    draft_id: str,
) -> ProductDraft:
    draft = await session.scalar(
        select(ProductDraft).where(
            ProductDraft.id == draft_id,
            ProductDraft.shop_binding_id == shop_binding_id,
        )
    )
    if draft is None:
        raise DraftNotFound("product draft was not found for this shop")
    return draft


async def save_product_draft(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    product: NormalizedProduct,
    source_kind: str,
    source_result_id: str | None = None,
    field_sources: Mapping[str, Any] | None = None,
) -> tuple[ProductDraft, bool]:
    selected_source = source_kind.strip().upper()
    if selected_source not in {"MANUAL", "COLLECTOR", "IMPORT"}:
        raise ValueError("unsupported product draft source")
    payload = normalized_product_to_payload(product)
    payload_hash = canonical_payload_hash(payload)
    existing = await session.scalar(
        select(ProductDraft).where(
            ProductDraft.shop_binding_id == shop_binding_id,
            ProductDraft.payload_hash == payload_hash,
        )
    )
    if existing is not None:
        return existing, False
    draft = ProductDraft(
        shop_binding_id=shop_binding_id,
        source_kind=selected_source,
        source_result_id=source_result_id.strip() if source_result_id else None,
        title=product.title,
        normalized_payload=payload,
        field_sources=dict(field_sources or product.source_trace),
        unmapped_warnings=list(product.unmapped_warnings),
        payload_hash=payload_hash,
        status=ProductDraftStatus.DRAFT.value,
        human_confirmed=False,
    )
    session.add(draft)
    await session.flush()
    return draft, True


async def confirm_product_draft(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    draft_id: str,
) -> ProductDraft:
    draft = await get_owned_draft(
        session,
        shop_binding_id=shop_binding_id,
        draft_id=draft_id,
    )
    if draft.status not in {ProductDraftStatus.DRAFT.value, ProductDraftStatus.READY.value}:
        raise DraftConflict("only an editable draft can be confirmed")
    product = normalized_product_from_payload(draft.normalized_payload)
    if not product.ready_for_listing:
        raise DraftConflict("draft still has missing listing facts or unresolved warnings")
    draft.human_confirmed = True
    draft.status = ProductDraftStatus.READY.value
    return draft


async def register_image_upload(
    session: AsyncSession,
    *,
    draft: ProductDraft,
    source_ref: str,
    content: bytes,
    content_type: str,
) -> tuple[ProductImageAsset, bool]:
    source_hash = source_reference_hash(source_ref)
    digest = content_sha256(content)
    existing = await session.scalar(
        select(ProductImageAsset).where(
            ProductImageAsset.product_draft_id == draft.id,
            ProductImageAsset.source_ref_hash == source_hash,
        )
    )
    if existing is not None:
        if existing.content_sha256 != digest:
            raise DraftConflict("image source reference was reused with different content")
        return existing, False
    asset = ProductImageAsset(
        product_draft_id=draft.id,
        source_ref_hash=source_hash,
        content_sha256=digest,
        content_type=content_type,
        byte_size=len(content),
        upload_state=WriteState.VALIDATING.value,
    )
    session.add(asset)
    await session.flush()
    return asset, True


async def complete_image_upload(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    draft_id: str,
    asset_id: str,
    source_ref: str,
    image_id: str,
    request_id: str | None,
) -> ProductDraft:
    draft = await get_owned_draft(
        session,
        shop_binding_id=shop_binding_id,
        draft_id=draft_id,
    )
    asset = await session.get(ProductImageAsset, asset_id)
    if asset is None or asset.product_draft_id != draft.id:
        raise DraftNotFound("product image asset was not found")
    product = normalized_product_from_payload(draft.normalized_payload)
    updated = bind_uploaded_image(product, source_url=source_ref, image_id=image_id)
    payload = normalized_product_to_payload(updated)
    draft.normalized_payload = payload
    draft.payload_hash = canonical_payload_hash(payload)
    asset.tiktok_image_id = image_id
    asset.platform_request_id = request_id
    asset.upload_state = WriteState.ACTIVE.value
    asset.last_error_code = None
    asset.last_error_redacted = None
    return draft


async def record_quota_snapshot(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    region: str,
    listing_limit: int | None,
    locally_submitted_count: int,
    confirmed_at: datetime,
    expires_at: datetime,
    stage: str | None = None,
    confirmed_by_session_id: str | None = None,
    source: str = "SELLER_CENTER_CONFIRMED",
) -> QuotaSnapshotModel:
    if listing_limit is not None and listing_limit < 0:
        raise ValueError("listing limit cannot be negative")
    if locally_submitted_count < 0:
        raise ValueError("submitted listing count cannot be negative")
    if listing_limit is not None and locally_submitted_count > listing_limit:
        raise ValueError("submitted listing count exceeds the confirmed limit")
    if _aware(expires_at) <= _aware(confirmed_at):
        raise ValueError("quota expiry must be after confirmation")
    if source != "SELLER_CENTER_CONFIRMED":
        raise ValueError("quota source must be explicitly confirmed in Seller Center")
    snapshot = QuotaSnapshotModel(
        shop_binding_id=shop_binding_id,
        region=region.strip().upper(),
        stage=stage.strip().upper() if stage else None,
        listing_limit=listing_limit,
        locally_submitted_count=locally_submitted_count,
        confirmed_at=confirmed_at,
        expires_at=expires_at,
        confirmed_by_session_id=confirmed_by_session_id,
        source=source,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def latest_quota_snapshot(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    region: str,
) -> QuotaSnapshotModel | None:
    return await session.scalar(
        select(QuotaSnapshotModel)
        .where(
            QuotaSnapshotModel.shop_binding_id == shop_binding_id,
            QuotaSnapshotModel.region == region,
        )
        .order_by(QuotaSnapshotModel.confirmed_at.desc(), QuotaSnapshotModel.id.desc())
        .limit(1)
    )


async def reserve_listing_quota(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    region: str,
    requested: int = 1,
    now: datetime | None = None,
) -> str:
    current = datetime.now(UTC) if now is None else now
    snapshot = await latest_quota_snapshot(
        session,
        shop_binding_id=shop_binding_id,
        region=region,
    )
    domain_snapshot = (
        None
        if snapshot is None
        else QuotaSnapshot(
            listing_limit=snapshot.listing_limit,
            submitted_count=snapshot.locally_submitted_count,
            confirmed_at=_aware(snapshot.confirmed_at),
            expires_at=_aware(snapshot.expires_at),
            source=snapshot.source,
        )
    )
    decision = decide_listing_quota(domain_snapshot, requested, now=current)
    if decision is not QuotaDecision.ALLOW or snapshot is None or snapshot.listing_limit is None:
        raise ListingQuotaBlocked(decision)
    result = await session.execute(
        update(QuotaSnapshotModel)
        .where(
            QuotaSnapshotModel.id == snapshot.id,
            QuotaSnapshotModel.expires_at > current,
            QuotaSnapshotModel.locally_submitted_count + requested <= snapshot.listing_limit,
        )
        .values(
            locally_submitted_count=QuotaSnapshotModel.locally_submitted_count + requested
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ListingQuotaBlocked(QuotaDecision.QUEUE)
    return snapshot.id


async def release_listing_quota(
    session: AsyncSession,
    *,
    snapshot_id: str,
    released: int = 1,
) -> bool:
    if released <= 0:
        raise ValueError("released quota must be positive")
    result = await session.execute(
        update(QuotaSnapshotModel)
        .where(
            QuotaSnapshotModel.id == snapshot_id,
            QuotaSnapshotModel.locally_submitted_count >= released,
        )
        .values(
            locally_submitted_count=QuotaSnapshotModel.locally_submitted_count - released
        )
    )
    return result.rowcount == 1


async def record_product_submission(
    session: AsyncSession,
    *,
    draft: ProductDraft,
    mode: ListingMode,
    region: str,
    product_id: str,
    request_id: str | None,
) -> ProductLink:
    product = normalized_product_from_payload(draft.normalized_payload)
    seller_sku = product.skus[0].seller_sku
    link = await session.scalar(
        select(ProductLink).where(
            ProductLink.shop_binding_id == draft.shop_binding_id,
            ProductLink.seller_sku == seller_sku,
        )
    )
    if link is None:
        link = ProductLink(
            shop_binding_id=draft.shop_binding_id,
            draft_id=draft.id,
            source_kind=draft.source_kind,
            source_product_id=draft.source_result_id,
            seller_sku=seller_sku,
        )
        session.add(link)
        await session.flush()
    if mode is ListingMode.LOCAL_REPLICATION:
        local_ids = dict(link.local_product_by_region)
        local_ids[region] = product_id
        link.local_product_by_region = local_ids
        state = await session.scalar(
            select(MarketProductState).where(
                MarketProductState.product_link_id == link.id,
                MarketProductState.region == region,
            )
        )
        if state is None:
            state = MarketProductState(product_link_id=link.id, region=region)
            session.add(state)
        state.local_product_id = product_id
        state.product_status = "PENDING"
        state.operation_state = WriteState.AUDITING.value
    else:
        link.global_product_id = product_id
    draft.status = ProductDraftStatus.SUBMITTED.value
    draft.human_confirmed = True
    # request_id is retained on the idempotent operation; product links contain
    # only stable business identifiers.
    _ = request_id
    return link