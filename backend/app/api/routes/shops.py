"""Protected, read-only registry of browser-safe shop capability facts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import AuthenticatedAdmin, require_admin_session, require_csrf
from app.api.dependencies import IdempotencyKey, ShopBindingId, database_session
from app.api.errors import ERROR_RESPONSES
from app.db.models import QuotaSnapshotModel, ScopeSnapshot, ShopBinding
from app.domain.enums import AuthorizationStatus, ListingMode, Scope
from app.use_cases.listing_mode import (
    ListingModeDecision,
    ManualListingModeConfirmation,
    assess_persisted_listing_mode,
    confirm_manual_listing_mode,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ShopQuotaResponse(_StrictModel):
    stage: str | None
    listing_limit: int | None
    locally_submitted_count: int
    confirmed_at: datetime
    expires_at: datetime


class ShopSummaryResponse(_StrictModel):
    id: str
    shop_id: str
    shop_code: str | None
    region: str
    seller_type: str | None
    shop_status: str
    kyc_status: str
    listing_mode: str
    authorization_status: str
    granted_scopes: list[str]
    missing_scopes: list[str]
    scope_captured_at: datetime | None
    access_expires_at: datetime | None
    quota: ShopQuotaResponse | None
    selectable: bool
    product_read_enabled: bool
    product_write_preconditions_met: bool
    order_read_enabled: bool
    product_read_blockers: list[str]
    product_write_blockers: list[str]
    order_read_blockers: list[str]


class ShopListResponse(_StrictModel):
    items: list[ShopSummaryResponse]


class ListingModeConfirmationRequest(_StrictModel):
    target_shop_id: str = Field(min_length=1, max_length=128)
    mode: ListingMode
    local_read_verified: bool
    global_read_verified: bool


class ListingModeDecisionResponse(_StrictModel):
    mode: ListingMode
    writable: bool
    evidence: list[str]
    blockers: list[str]
    recorded_evidence_id: str | None = None


router = APIRouter(prefix="/api/shops", tags=["shops"], responses=ERROR_RESPONSES)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _authorization_blockers(binding: ShopBinding) -> list[str]:
    return (
        []
        if binding.authorization_status == AuthorizationStatus.ACTIVE.value
        else ["BLOCKED_SHOP_AUTHORIZATION"]
    )


def _token_blockers(scope: ScopeSnapshot | None, now: datetime) -> list[str]:
    if scope is None:
        return ["BLOCKED_SCOPE_SNAPSHOT"]
    if scope.access_expires_at is None:
        return ["BLOCKED_TOKEN_EXPIRY_UNKNOWN"]
    if _utc(scope.access_expires_at) <= now:
        return ["BLOCKED_ACCESS_TOKEN_EXPIRED"]
    return []


def _scope_blocker(
    scope: ScopeSnapshot | None,
    required: Scope | None,
    blocker: str,
) -> list[str]:
    if required is None or scope is None:
        return []
    return [] if required.value in set(scope.granted_scopes) else [blocker]


def _mode_scope(mode: str, *, write: bool) -> Scope | None:
    if mode == ListingMode.LOCAL_REPLICATION.value:
        return Scope.PRODUCT_WRITE if write else Scope.PRODUCT_BASIC
    if mode == ListingMode.GLOBAL_LEGACY.value:
        return Scope.GLOBAL_PRODUCT_WRITE if write else Scope.GLOBAL_PRODUCT_INFO
    return None


def _quota_blockers(
    binding: ShopBinding,
    quota: QuotaSnapshotModel | None,
    now: datetime,
) -> list[str]:
    if binding.listing_mode != ListingMode.LOCAL_REPLICATION.value:
        return []
    if quota is None:
        return ["BLOCKED_QUOTA_CONFIRMATION"]
    if _utc(quota.expires_at) <= now:
        return ["BLOCKED_QUOTA_EXPIRED"]
    if (
        quota.listing_limit is not None
        and quota.locally_submitted_count >= quota.listing_limit
    ):
        return ["BLOCKED_QUOTA_EXHAUSTED"]
    return []


def _summary(
    binding: ShopBinding,
    scope: ScopeSnapshot | None,
    quota: QuotaSnapshotModel | None,
    now: datetime,
) -> ShopSummaryResponse:
    authorization = _authorization_blockers(binding)
    token = _token_blockers(scope, now)
    unknown_mode = (
        ["BLOCKED_LISTING_MODE_UNKNOWN"]
        if binding.listing_mode == ListingMode.UNKNOWN.value
        else []
    )
    product_read_blockers = [
        *authorization,
        *token,
        *unknown_mode,
        *_scope_blocker(
            scope,
            _mode_scope(binding.listing_mode, write=False),
            "BLOCKED_PRODUCT_READ_SCOPE",
        ),
    ]
    product_write_blockers = [
        *authorization,
        *token,
        *unknown_mode,
        *_scope_blocker(
            scope,
            _mode_scope(binding.listing_mode, write=True),
            "BLOCKED_PRODUCT_WRITE_SCOPE",
        ),
        *_quota_blockers(binding, quota, now),
    ]
    order_read_blockers = [
        *authorization,
        *token,
        *_scope_blocker(scope, Scope.ORDER_INFO, "BLOCKED_ORDER_SCOPE"),
    ]
    return ShopSummaryResponse(
        id=binding.id,
        shop_id=binding.shop_id,
        shop_code=binding.shop_code,
        region=binding.region,
        seller_type=binding.seller_type,
        shop_status=binding.shop_status,
        kyc_status=binding.kyc_status,
        listing_mode=binding.listing_mode,
        authorization_status=binding.authorization_status,
        granted_scopes=sorted(scope.granted_scopes) if scope is not None else [],
        missing_scopes=sorted(scope.missing_scopes) if scope is not None else [],
        scope_captured_at=scope.captured_at if scope is not None else None,
        access_expires_at=scope.access_expires_at if scope is not None else None,
        quota=(
            ShopQuotaResponse(
                stage=quota.stage,
                listing_limit=quota.listing_limit,
                locally_submitted_count=quota.locally_submitted_count,
                confirmed_at=quota.confirmed_at,
                expires_at=quota.expires_at,
            )
            if quota is not None
            else None
        ),
        selectable=not authorization,
        product_read_enabled=not product_read_blockers,
        product_write_preconditions_met=not product_write_blockers,
        order_read_enabled=not order_read_blockers,
        product_read_blockers=product_read_blockers,
        product_write_blockers=product_write_blockers,
        order_read_blockers=order_read_blockers,
    )


def _listing_mode_response(
    decision: ListingModeDecision,
    *,
    recorded_evidence_id: str | None = None,
) -> ListingModeDecisionResponse:
    return ListingModeDecisionResponse(
        mode=decision.mode,
        writable=decision.writable,
        evidence=list(decision.evidence),
        blockers=list(decision.blockers),
        recorded_evidence_id=recorded_evidence_id,
    )


@router.get(
    "/{shop_binding_id}/listing-mode",
    response_model=ListingModeDecisionResponse,
)
async def get_listing_mode(
    shop_binding_id: ShopBindingId,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    session: Annotated[AsyncSession, Depends(database_session)],
) -> ListingModeDecisionResponse:
    decision = await assess_persisted_listing_mode(
        session,
        shop_binding_id=shop_binding_id,
    )
    return _listing_mode_response(decision)


@router.post(
    "/{shop_binding_id}/listing-mode-confirmations",
    response_model=ListingModeDecisionResponse,
    status_code=201,
)
async def record_listing_mode_confirmation(
    shop_binding_id: ShopBindingId,
    payload: ListingModeConfirmationRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(database_session)],
) -> ListingModeDecisionResponse:
    result = await confirm_manual_listing_mode(
        session,
        shop_binding_id=shop_binding_id,
        actor_session_id=admin.session_id,
        idempotency_key=idempotency_key,
        confirmation=ManualListingModeConfirmation(
            target_shop_id=payload.target_shop_id,
            mode=payload.mode,
            local_read_verified=payload.local_read_verified,
            global_read_verified=payload.global_read_verified,
        ),
    )
    if result.replayed:
        response.status_code = 200
    return _listing_mode_response(
        result.decision,
        recorded_evidence_id=result.recorded_evidence_id,
    )


@router.get("", response_model=ShopListResponse)
async def list_shops(
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    session: Annotated[AsyncSession, Depends(database_session)],
) -> ShopListResponse:
    bindings = tuple(
        await session.scalars(
            select(ShopBinding).order_by(ShopBinding.created_at, ShopBinding.id).limit(100)
        )
    )
    if not bindings:
        return ShopListResponse(items=[])
    binding_ids = [binding.id for binding in bindings]
    scopes = tuple(
        await session.scalars(
            select(ScopeSnapshot)
            .where(ScopeSnapshot.shop_binding_id.in_(binding_ids))
            .order_by(
                ScopeSnapshot.shop_binding_id,
                ScopeSnapshot.captured_at.desc(),
                ScopeSnapshot.id.desc(),
            )
        )
    )
    quotas = tuple(
        await session.scalars(
            select(QuotaSnapshotModel)
            .where(QuotaSnapshotModel.shop_binding_id.in_(binding_ids))
            .order_by(
                QuotaSnapshotModel.shop_binding_id,
                QuotaSnapshotModel.confirmed_at.desc(),
                QuotaSnapshotModel.id.desc(),
            )
        )
    )
    latest_scope: dict[str, ScopeSnapshot] = {}
    latest_quota: dict[str, QuotaSnapshotModel] = {}
    for scope in scopes:
        latest_scope.setdefault(scope.shop_binding_id, scope)
    for quota in quotas:
        latest_quota.setdefault(quota.shop_binding_id, quota)
    now = datetime.now(UTC)
    return ShopListResponse(
        items=[
            _summary(
                binding,
                latest_scope.get(binding.id),
                latest_quota.get(binding.id),
                now,
            )
            for binding in bindings
        ]
    )