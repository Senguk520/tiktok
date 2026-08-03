"""Protected, read-only registry of browser-safe shop capability facts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import AuthenticatedAdmin, require_admin_session
from app.api.dependencies import database_session
from app.api.errors import ERROR_RESPONSES
from app.db.models import QuotaSnapshotModel, ScopeSnapshot, ShopBinding
from app.domain.enums import AuthorizationStatus, ListingMode, Scope


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