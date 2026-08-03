"""Protected, PII-minimized order query and resumable synchronization routes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import AuthenticatedAdmin, require_admin_session, require_csrf
from app.api.dependencies import (
    ShopBindingId,
    commerce_runtime,
    database_session,
    session_factory,
    shop_access_context,
)
from app.api.errors import ERROR_RESPONSES
from app.api.runtime import CommerceRuntime
from app.db.models import OrderLineRecord, OrderRecord
from app.domain.orders import NormalizedOrder, NormalizedOrderLine
from app.use_cases.commerce_context import ShopAccessContext


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderLineResponse(_StrictModel):
    line_id: str
    product_id: str | None
    sku_id: str | None
    seller_sku: str | None
    status: str | None
    quantity: int
    currency: str | None
    sale_price: Decimal | None


class OrderResponse(_StrictModel):
    order_id: str
    status: str
    fulfillment_type: str | None
    shipping_type: str | None
    currency: str | None
    total_amount: Decimal | None
    item_count: int
    created_at: datetime | None
    updated_at: datetime | None
    lines: list[OrderLineResponse]


class OrderPageResponse(_StrictModel):
    orders: list[OrderResponse]
    next_page_token: str | None
    total_count: int | None
    request_id: str | None


class OrderDetailsResponse(_StrictModel):
    orders: list[OrderResponse]


class OrderSyncRequest(_StrictModel):
    window_start: datetime
    window_end: datetime
    page_size: int = Field(default=100, ge=1, le=100)
    max_pages: int = Field(default=100, ge=1, le=1000)


class OrderSyncResponse(_StrictModel):
    pages: int
    listed_orders: int
    detailed_orders: int
    completed: bool
    next_page_token: str | None
    window_start: datetime
    window_end: datetime


def _ids(values: list[str]) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values if value.strip())
    if not cleaned or len(cleaned) > 50 or len(cleaned) != len(set(cleaned)):
        raise ValueError("order ids must contain 1-50 unique values")
    return cleaned


def _line_response(line: NormalizedOrderLine) -> OrderLineResponse:
    return OrderLineResponse(
        line_id=line.line_id,
        product_id=line.product_id,
        sku_id=line.sku_id,
        seller_sku=line.seller_sku,
        status=line.status,
        quantity=line.quantity,
        currency=line.currency,
        sale_price=line.sale_price,
    )


def _order_response(order: NormalizedOrder) -> OrderResponse:
    return OrderResponse(
        order_id=order.order_id,
        status=order.status,
        fulfillment_type=order.fulfillment_type,
        shipping_type=order.shipping_type,
        currency=order.currency,
        total_amount=order.total_amount,
        item_count=order.item_count,
        created_at=order.source_created_at,
        updated_at=order.source_updated_at,
        lines=[_line_response(line) for line in order.lines],
    )


def _stored_order_response(
    record: OrderRecord,
    lines: tuple[OrderLineRecord, ...],
) -> OrderResponse:
    return OrderResponse(
        order_id=record.platform_order_id,
        status=record.order_status,
        fulfillment_type=record.fulfillment_type,
        shipping_type=record.shipping_type,
        currency=record.currency,
        total_amount=Decimal(record.total_amount) if record.total_amount is not None else None,
        item_count=record.item_count,
        created_at=record.source_created_at,
        updated_at=record.source_updated_at,
        lines=[
            OrderLineResponse(
                line_id=line.platform_line_id,
                product_id=line.product_id,
                sku_id=line.sku_id,
                seller_sku=line.seller_sku,
                status=line.line_status,
                quantity=line.quantity,
                currency=line.currency,
                sale_price=Decimal(line.sale_price) if line.sale_price is not None else None,
            )
            for line in lines
        ],
    )


router = APIRouter(
    prefix="/api/shops/{shop_binding_id}/orders",
    tags=["orders"],
    responses=ERROR_RESPONSES,
)


@router.get("/remote", response_model=OrderPageResponse)
async def search_remote_orders(
    shop_binding_id: ShopBindingId,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    context: Annotated[ShopAccessContext, Depends(shop_access_context)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    page_token: Annotated[str | None, Query(max_length=512)] = None,
) -> OrderPageResponse:
    del shop_binding_id
    page = await runtime.order_service.fetch_page(
        context,
        page_size=page_size,
        page_token=page_token,
    )
    return OrderPageResponse(
        orders=[_order_response(order) for order in page.orders],
        next_page_token=page.next_page_token,
        total_count=page.total_count,
        request_id=page.request_id,
    )


@router.get("/remote/details", response_model=OrderDetailsResponse)
async def remote_order_details(
    shop_binding_id: ShopBindingId,
    ids: Annotated[list[str], Query(min_length=1, max_length=50)],
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    context: Annotated[ShopAccessContext, Depends(shop_access_context)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> OrderDetailsResponse:
    del shop_binding_id
    orders = await runtime.order_service.fetch_details(context, _ids(ids))
    return OrderDetailsResponse(orders=[_order_response(order) for order in orders])


@router.get("/local/details", response_model=OrderDetailsResponse)
async def local_order_details(
    shop_binding_id: ShopBindingId,
    ids: Annotated[list[str], Query(min_length=1, max_length=50)],
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    session: Annotated[AsyncSession, Depends(database_session)],
    context: Annotated[ShopAccessContext, Depends(shop_access_context)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> OrderDetailsResponse:
    del shop_binding_id
    records = await runtime.order_service.read_local_details(session, context, _ids(ids))
    return OrderDetailsResponse(
        orders=[_stored_order_response(record, lines) for record, lines in records]
    )


@router.post("/sync", response_model=OrderSyncResponse)
async def sync_orders(
    shop_binding_id: ShopBindingId,
    payload: OrderSyncRequest,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
    context: Annotated[ShopAccessContext, Depends(shop_access_context)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> OrderSyncResponse:
    del shop_binding_id
    summary = await runtime.order_service.sync_window(
        factory,
        context,
        window_start=payload.window_start,
        window_end=payload.window_end,
        page_size=payload.page_size,
        max_pages=payload.max_pages,
    )
    return OrderSyncResponse(
        pages=summary.pages,
        listed_orders=summary.listed_orders,
        detailed_orders=summary.detailed_orders,
        completed=summary.completed,
        next_page_token=summary.next_page_token,
        window_start=summary.window_start,
        window_end=summary.window_end,
    )