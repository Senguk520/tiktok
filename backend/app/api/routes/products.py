"""Protected product draft and read-only remote product routes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import AuthenticatedAdmin, require_admin_session, require_csrf
from app.api.dependencies import (
    UUID_PATTERN,
    IdempotencyKey,
    ShopBindingId,
    commerce_runtime,
    database_session,
    session_factory,
    shop_access_context,
)
from app.api.errors import ERROR_RESPONSES, ApiProblem
from app.api.runtime import CommerceRuntime
from app.db.models import ProductDraft, QuotaSnapshotModel
from app.domain.product import NormalizedImage, NormalizedProduct, NormalizedSku
from app.domain.product_payload import normalized_product_from_payload
from app.integrations.tiktok.products import ProductGatewayError, ProductPage
from app.use_cases.commerce_context import ShopAccessContext
from app.use_cases.product_capabilities import evaluate_product_capabilities
from shared.redaction import is_sensitive_key


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductImageInput(_StrictModel):
    source_ref: str = Field(min_length=1, max_length=2048)
    role: str = Field(default="MAIN", min_length=1, max_length=64)


class ProductSkuInput(_StrictModel):
    seller_sku: str = Field(min_length=1, max_length=128)
    price: Decimal = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    inventory_by_warehouse: dict[str, int] = Field(min_length=1, max_length=100)
    attributes: dict[str, str] = Field(default_factory=dict, max_length=200)

    @field_validator("inventory_by_warehouse")
    @classmethod
    def validate_inventory(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not key.strip() or quantity < 0 for key, quantity in value.items()):
            raise ValueError("warehouse ids must be non-empty and quantities non-negative")
        return value

    @field_validator("attributes")
    @classmethod
    def reject_sensitive_attributes(cls, value: dict[str, str]) -> dict[str, str]:
        if any(is_sensitive_key(key) for key in value):
            raise ValueError("sensitive keys are not accepted in product attributes")
        return value


class ProductDraftCreate(_StrictModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=20_000)
    category_id: str | None = Field(default=None, min_length=1, max_length=128)
    skus: list[ProductSkuInput] = Field(min_length=1, max_length=100)
    images: list[ProductImageInput] = Field(default_factory=list, max_length=20)
    attributes: dict[str, str] = Field(default_factory=dict, max_length=200)
    unmapped_warnings: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("attributes")
    @classmethod
    def reject_sensitive_attributes(cls, value: dict[str, str]) -> dict[str, str]:
        if any(is_sensitive_key(key) for key in value):
            raise ValueError("sensitive keys are not accepted in product attributes")
        return value

    @field_validator("unmapped_warnings")
    @classmethod
    def validate_warnings(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 500 for item in value):
            raise ValueError("unmapped warnings must be bounded non-empty text")
        return value


class ProductImageResponse(_StrictModel):
    source_ref: str
    role: str
    platform_image_bound: bool


class ProductSkuResponse(_StrictModel):
    seller_sku: str
    price: Decimal
    currency: str
    inventory_by_warehouse: dict[str, int]
    attributes: dict[str, str]


class ProductIntentResponse(_StrictModel):
    title: str
    description: str
    category_id: str | None
    skus: list[ProductSkuResponse]
    images: list[ProductImageResponse]
    attributes: dict[str, str]
    unmapped_warnings: list[str]


class DraftResponse(_StrictModel):
    id: str
    status: str
    human_confirmed: bool
    created: bool | None = None
    product: ProductIntentResponse


class QuotaConfirmationRequest(_StrictModel):
    listing_limit: int | None = Field(default=None, ge=0)
    locally_submitted_count: int = Field(ge=0)
    confirmed_at: datetime
    expires_at: datetime
    stage: str | None = Field(default=None, max_length=32)


class QuotaResponse(_StrictModel):
    id: str
    region: str
    listing_limit: int | None
    locally_submitted_count: int
    confirmed_at: datetime
    expires_at: datetime
    source: str


class SubmissionResponse(_StrictModel):
    mode: str
    product_id: str
    operation_id: str
    request_id: str | None
    replayed: bool


class RemoteProductSummary(_StrictModel):
    product_id: str
    title: str | None
    status: str | None
    seller_skus: list[str]


class RemoteProductPageResponse(_StrictModel):
    mode: str
    items: list[RemoteProductSummary]
    next_page_token: str | None
    total_count: int | None
    request_id: str | None


class ProductCapabilitiesResponse(_StrictModel):
    platform_configured: bool
    master_key_configured: bool
    listing_mode: str
    image_upload_enabled: bool
    product_submission_enabled: bool
    image_upload_blockers: list[str]
    product_submission_blockers: list[str]
    blockers: list[str]


def _intent(payload: ProductDraftCreate) -> NormalizedProduct:
    return NormalizedProduct(
        title=payload.title.strip(),
        description=payload.description,
        category_id=payload.category_id.strip() if payload.category_id else None,
        skus=tuple(
            NormalizedSku(
                seller_sku=sku.seller_sku.strip(),
                price=sku.price,
                currency=sku.currency.upper(),
                inventory_by_warehouse={key.strip(): value for key, value in sku.inventory_by_warehouse.items()},
                attributes={key.strip(): value.strip() for key, value in sku.attributes.items()},
            )
            for sku in payload.skus
        ),
        images=tuple(
            NormalizedImage(source_url=image.source_ref.strip(), role=image.role.strip().upper())
            for image in payload.images
        ),
        attributes={key.strip(): value.strip() for key, value in payload.attributes.items()},
        unmapped_warnings=tuple(item.strip() for item in payload.unmapped_warnings),
    )


def _intent_response(product: NormalizedProduct) -> ProductIntentResponse:
    return ProductIntentResponse(
        title=product.title,
        description=product.description,
        category_id=product.category_id,
        skus=[
            ProductSkuResponse(
                seller_sku=sku.seller_sku,
                price=sku.price,
                currency=sku.currency,
                inventory_by_warehouse=dict(sku.inventory_by_warehouse),
                attributes=dict(sku.attributes),
            )
            for sku in product.skus
        ],
        images=[
            ProductImageResponse(
                source_ref=image.source_url,
                role=image.role,
                platform_image_bound=image.local_image_id is not None,
            )
            for image in product.images
        ],
        attributes=dict(product.attributes),
        unmapped_warnings=list(product.unmapped_warnings),
    )


def _draft_response(draft: ProductDraft, *, created: bool | None = None) -> DraftResponse:
    return DraftResponse(
        id=draft.id,
        status=draft.status,
        human_confirmed=draft.human_confirmed,
        created=created,
        product=_intent_response(normalized_product_from_payload(draft.normalized_payload)),
    )


def _remote_summary(item: dict[str, Any] | Any) -> RemoteProductSummary:
    if not isinstance(item, dict):
        item = dict(item)
    identifier = item.get("product_id") or item.get("global_product_id") or item.get("id")
    if not isinstance(identifier, (str, int)) or not str(identifier).strip():
        raise ProductGatewayError("TikTok product response lacks a stable product id")
    raw_skus = item.get("skus", [])
    seller_skus: list[str] = []
    if isinstance(raw_skus, list):
        for sku in raw_skus:
            if isinstance(sku, dict) and sku.get("seller_sku"):
                seller_skus.append(str(sku["seller_sku"]))
    return RemoteProductSummary(
        product_id=str(identifier),
        title=str(item["title"]) if item.get("title") is not None else None,
        status=str(item["status"]) if item.get("status") is not None else None,
        seller_skus=seller_skus,
    )


def _remote_page(page: ProductPage) -> RemoteProductPageResponse:
    return RemoteProductPageResponse(
        mode=page.mode.value,
        items=[_remote_summary(item) for item in page.items],
        next_page_token=page.next_page_token,
        total_count=page.total_count,
        request_id=page.request_id,
    )


router = APIRouter(
    prefix="/api/shops/{shop_binding_id}/products",
    tags=["products"],
    responses=ERROR_RESPONSES,
)


@router.get("/capabilities", response_model=ProductCapabilitiesResponse)
async def product_capabilities(
    shop_binding_id: ShopBindingId,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    session: Annotated[AsyncSession, Depends(database_session)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> ProductCapabilitiesResponse:
    decision = await evaluate_product_capabilities(
        session,
        shop_binding_id=shop_binding_id,
        platform_configured=runtime.platform_configured,
        key_ring=runtime.key_ring,
        endpoint_evidence=runtime.product_capabilities,
    )
    return ProductCapabilitiesResponse(
        platform_configured=decision.platform_configured,
        master_key_configured=decision.master_key_configured,
        listing_mode=decision.listing_mode.value,
        image_upload_enabled=decision.image_upload_enabled,
        product_submission_enabled=decision.product_submission_enabled,
        image_upload_blockers=list(decision.image_upload_blockers),
        product_submission_blockers=list(decision.product_submission_blockers),
        blockers=list(decision.blockers),
    )


@router.post("/drafts", response_model=DraftResponse, status_code=201)
async def create_draft(
    shop_binding_id: ShopBindingId,
    payload: ProductDraftCreate,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(database_session)],
    context: Annotated[ShopAccessContext, Depends(shop_access_context)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> DraftResponse:
    del shop_binding_id
    result = await runtime.product_service.save_draft(session, context, _intent(payload))
    return _draft_response(result.draft, created=result.created)


@router.post("/drafts/{draft_id}/confirm", response_model=DraftResponse)
async def confirm_draft(
    shop_binding_id: ShopBindingId,
    draft_id: Annotated[str, Path(min_length=36, max_length=36, pattern=UUID_PATTERN)],
    _admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(database_session)],
    context: Annotated[ShopAccessContext, Depends(shop_access_context)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> DraftResponse:
    del shop_binding_id
    draft = await runtime.product_service.confirm_draft(session, context, draft_id)
    return _draft_response(draft)


@router.post("/quota-confirmations", response_model=QuotaResponse, status_code=201)
async def confirm_quota(
    shop_binding_id: ShopBindingId,
    payload: QuotaConfirmationRequest,
    admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(database_session)],
    context: Annotated[ShopAccessContext, Depends(shop_access_context)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> QuotaResponse:
    del shop_binding_id
    snapshot: QuotaSnapshotModel = await runtime.product_service.confirm_quota(
        session,
        context,
        listing_limit=payload.listing_limit,
        locally_submitted_count=payload.locally_submitted_count,
        confirmed_at=payload.confirmed_at,
        expires_at=payload.expires_at,
        stage=payload.stage,
        confirmed_by_session_id=admin.session_id,
    )
    return QuotaResponse.model_validate(snapshot, from_attributes=True)


@router.post("/drafts/{draft_id}/submit", response_model=SubmissionResponse)
async def submit_draft(
    shop_binding_id: ShopBindingId,
    draft_id: Annotated[str, Path(min_length=36, max_length=36, pattern=UUID_PATTERN)],
    idempotency_key: IdempotencyKey,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
    context: Annotated[ShopAccessContext, Depends(shop_access_context)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> SubmissionResponse:
    del shop_binding_id
    result = await runtime.product_service.submit_draft(
        factory,
        context,
        draft_id=draft_id,
        idempotency_key=idempotency_key,
    )
    return SubmissionResponse(
        mode=result.submission.mode.value,
        product_id=result.submission.product_id,
        operation_id=result.operation_id,
        request_id=result.submission.request_id,
        replayed=result.replayed,
    )


@router.get("/remote", response_model=RemoteProductPageResponse)
async def search_remote_products(
    shop_binding_id: ShopBindingId,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    context: Annotated[ShopAccessContext, Depends(shop_access_context)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    page_token: Annotated[str | None, Query(max_length=512)] = None,
) -> RemoteProductPageResponse:
    del shop_binding_id
    if runtime.product_gateway is None:
        raise ApiProblem(503, "BLOCKED_LIVE_CREDENTIALS", "TikTok platform credentials are not configured")
    return _remote_page(
        await runtime.product_gateway.search(
            context,
            page_size=page_size,
            page_token=page_token,
        )
    )


@router.get("/remote/{product_id}", response_model=RemoteProductSummary)
async def get_remote_product(
    shop_binding_id: ShopBindingId,
    product_id: Annotated[str, Path(min_length=1, max_length=128)],
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    context: Annotated[ShopAccessContext, Depends(shop_access_context)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> RemoteProductSummary:
    del shop_binding_id
    if runtime.product_gateway is None:
        raise ApiProblem(503, "BLOCKED_LIVE_CREDENTIALS", "TikTok platform credentials are not configured")
    return _remote_summary(dict(await runtime.product_gateway.get(context, product_id)))