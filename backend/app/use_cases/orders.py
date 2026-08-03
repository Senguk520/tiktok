"""Resumable order synchronization with 50-id detail batches and PII-free storage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import Scope
from app.domain.orders import NormalizedOrder, NormalizedOrderPage
from app.repositories.orders import (
    get_sync_checkpoint,
    local_order_details,
    save_sync_checkpoint,
    upsert_orders,
)
from app.use_cases.commerce_context import ShopAccessContext


class OrderGateway(Protocol):
    async def search(
        self,
        context: ShopAccessContext,
        *,
        page_size: int = 20,
        page_token: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> NormalizedOrderPage: ...

    async def details(
        self,
        context: ShopAccessContext,
        order_ids: Sequence[str],
    ) -> tuple[NormalizedOrder, ...]: ...


@dataclass(frozen=True, slots=True)
class OrderSyncSummary:
    pages: int
    listed_orders: int
    detailed_orders: int
    completed: bool
    next_page_token: str | None
    window_start: datetime
    window_end: datetime


def detail_batches(order_ids: Sequence[str], *, size: int = 50) -> tuple[tuple[str, ...], ...]:
    if not 1 <= size <= 50:
        raise ValueError("order detail batch size must be between 1 and 50")
    cleaned = tuple(value.strip() for value in order_ids if value.strip())
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("order ids must be unique before batching")
    return tuple(cleaned[index : index + size] for index in range(0, len(cleaned), size))


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sync_filters(window_start: datetime, window_end: datetime) -> dict[str, int]:
    return {
        "update_time_ge": int(window_start.timestamp()),
        "update_time_lt": int(window_end.timestamp()),
    }


class OrderService:
    def __init__(self, gateway: OrderGateway) -> None:
        self._gateway = gateway

    async def fetch_page(
        self,
        context: ShopAccessContext,
        *,
        page_size: int = 20,
        page_token: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> NormalizedOrderPage:
        context.require_scopes(Scope.ORDER_INFO)
        return await self._gateway.search(
            context,
            page_size=page_size,
            page_token=page_token,
            filters=filters,
        )

    async def fetch_details(
        self,
        context: ShopAccessContext,
        order_ids: Sequence[str],
    ) -> tuple[NormalizedOrder, ...]:
        context.require_scopes(Scope.ORDER_INFO)
        results: list[NormalizedOrder] = []
        for batch in detail_batches(order_ids):
            results.extend(await self._gateway.details(context, batch))
        return tuple(results)

    async def read_local_details(
        self,
        session: AsyncSession,
        context: ShopAccessContext,
        order_ids: Sequence[str],
    ):
        context.require_active()
        return await local_order_details(
            session,
            shop_binding_id=context.shop_binding_id,
            order_ids=order_ids,
        )

    async def sync_window(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        context: ShopAccessContext,
        *,
        window_start: datetime,
        window_end: datetime,
        page_size: int = 100,
        max_pages: int = 100,
        stream_name: str = "orders.updated",
    ) -> OrderSyncSummary:
        """Fetch outside DB transactions, then atomically persist each page and checkpoint."""

        context.require_scopes(Scope.ORDER_INFO)
        start = _utc(window_start, field="window_start")
        end = _utc(window_end, field="window_end")
        if start >= end:
            raise ValueError("order sync window must be non-empty")
        if not 1 <= page_size <= 100 or max_pages <= 0:
            raise ValueError("invalid order sync page limits")

        page_token: str | None = None
        async with session_factory() as session:
            checkpoint = await get_sync_checkpoint(
                session,
                shop_binding_id=context.shop_binding_id,
                stream_name=stream_name,
            )
            if (
                checkpoint is not None
                and checkpoint.page_token
                and checkpoint.window_start is not None
                and checkpoint.window_end is not None
                and _stored_utc(checkpoint.window_start) == start
                and _stored_utc(checkpoint.window_end) == end
            ):
                page_token = checkpoint.page_token

        pages = 0
        listed_count = 0
        detailed_count = 0
        seen_tokens: set[str] = set()
        filters = _sync_filters(start, end)
        while pages < max_pages:
            if page_token:
                if page_token in seen_tokens:
                    raise RuntimeError("TikTok order pagination token repeated")
                seen_tokens.add(page_token)
            page = await self._gateway.search(
                context,
                page_size=page_size,
                page_token=page_token,
                filters=filters,
            )
            ids = tuple(order.order_id for order in page.orders)
            details: list[NormalizedOrder] = []
            for batch in detail_batches(ids):
                details.extend(await self._gateway.details(context, batch))
            pages += 1
            listed_count += len(page.orders)
            detailed_count += len(details)
            completed = page.next_page_token is None
            if page.next_page_token is not None and page.next_page_token == page_token:
                raise RuntimeError("TikTok order pagination token did not advance")
            summary = {
                "pages": pages,
                "listed_orders": listed_count,
                "detailed_orders": detailed_count,
                "completed": completed,
                "last_request_id": page.request_id,
            }
            async with session_factory.begin() as session:
                await upsert_orders(
                    session,
                    shop_binding_id=context.shop_binding_id,
                    orders=page.orders,
                    detail=False,
                )
                await upsert_orders(
                    session,
                    shop_binding_id=context.shop_binding_id,
                    orders=details,
                    detail=True,
                )
                await save_sync_checkpoint(
                    session,
                    shop_binding_id=context.shop_binding_id,
                    page_token=page.next_page_token,
                    window_start=start,
                    window_end=end,
                    summary=summary,
                    completed=completed,
                    stream_name=stream_name,
                )
            page_token = page.next_page_token
            if completed:
                return OrderSyncSummary(
                    pages=pages,
                    listed_orders=listed_count,
                    detailed_orders=detailed_count,
                    completed=True,
                    next_page_token=None,
                    window_start=start,
                    window_end=end,
                )
        return OrderSyncSummary(
            pages=pages,
            listed_orders=listed_count,
            detailed_orders=detailed_count,
            completed=False,
            next_page_token=page_token,
            window_start=start,
            window_end=end,
        )