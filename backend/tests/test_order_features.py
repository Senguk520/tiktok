from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.db.base import DatabaseSettings, create_engine_and_session_factory
from app.db.models import OrderLineRecord, OrderRecord, OrderSyncCheckpoint, ShopBinding
from app.domain.enums import AuthorizationStatus, ListingMode, Scope
from app.domain.orders import (
    NormalizedOrder,
    NormalizedOrderLine,
    NormalizedOrderPage,
    normalize_order,
    normalized_order_to_payload,
)
from app.domain.scopes import ScopeSet
from app.integrations.tiktok.client import TikTokResult
from app.integrations.tiktok.orders import TikTokOrderGateway
from app.repositories.orders import upsert_orders
from app.use_cases.commerce_context import CommerceAccessBlocked, ShopAccessContext
from app.use_cases.orders import OrderService, detail_batches
from migrations.core import migrate_engine


class CapturingTikTokClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, endpoint_key: str, **kwargs: Any) -> TikTokResult:
        self.calls.append((endpoint_key, kwargs))
        if endpoint_key == "orders.search":
            return TikTokResult(
                data={
                    "orders": [
                        {
                            "id": "order-gateway",
                            "status": "UNPAID",
                            "payment": {"total_amount": "8.00", "currency": "MYR"},
                        }
                    ],
                    "next_page_token": "next-token",
                    "total_count": 1,
                },
                request_id="gateway-search",
            )
        return TikTokResult(
            data={
                "orders": [
                    {
                        "id": "order-gateway",
                        "status": "UNPAID",
                        "line_items": [{"id": "gateway-line", "quantity": 1}],
                    }
                ]
            },
            request_id="gateway-detail",
        )


class FakeOrderGateway:
    def __init__(self) -> None:
        self.search_tokens: list[str | None] = []
        self.detail_batches: list[tuple[str, ...]] = []

    async def search(
        self,
        context: ShopAccessContext,
        *,
        page_size: int = 20,
        page_token: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> NormalizedOrderPage:
        self.search_tokens.append(page_token)
        assert page_size == 100
        assert filters and filters["update_time_ge"] < filters["update_time_lt"]
        if page_token is None:
            return NormalizedOrderPage(
                orders=(summary_order("order-1", "AWAITING_SHIPMENT"),),
                next_page_token="page-2",
                total_count=2,
                request_id="search-1",
            )
        if page_token == "page-2":
            return NormalizedOrderPage(
                orders=(summary_order("order-2", "SHIPPED"),),
                next_page_token=None,
                total_count=2,
                request_id="search-2",
            )
        raise AssertionError(f"unexpected page token: {page_token}")

    async def details(
        self,
        context: ShopAccessContext,
        order_ids: tuple[str, ...],
    ) -> tuple[NormalizedOrder, ...]:
        self.detail_batches.append(tuple(order_ids))
        return tuple(detail_order(order_id) for order_id in order_ids)


def summary_order(order_id: str, status: str) -> NormalizedOrder:
    return NormalizedOrder(
        order_id=order_id,
        status=status,
        currency="MYR",
        total_amount=Decimal("29.90"),
        source_updated_at=datetime(2026, 3, 1, tzinfo=UTC),
    )


def detail_order(order_id: str) -> NormalizedOrder:
    return NormalizedOrder(
        order_id=order_id,
        status="SHIPPED" if order_id == "order-2" else "AWAITING_SHIPMENT",
        lines=(
            NormalizedOrderLine(
                line_id=f"line-{order_id}",
                product_id="product-1",
                sku_id="sku-1",
                seller_sku="SELLER-SKU-1",
                status="READY_TO_SHIP",
                quantity=2,
                currency="MYR",
                sale_price=Decimal("14.95"),
            ),
        ),
        lines_present=True,
        fulfillment_type="FULFILLMENT_BY_SELLER",
        shipping_type="TIKTOK_SHIPPING",
        currency="MYR",
        total_amount=Decimal("29.90"),
        source_created_at=datetime(2026, 2, 28, tzinfo=UTC),
        source_updated_at=datetime(2026, 3, 1, tzinfo=UTC),
    )


def order_context(binding: ShopBinding, *, with_scope: bool = True) -> ShopAccessContext:
    return ShopAccessContext(
        shop_binding_id=binding.id,
        shop_id=binding.shop_id,
        region=binding.region,
        listing_mode=ListingMode.UNKNOWN,
        authorization_status=AuthorizationStatus.ACTIVE,
        scopes=ScopeSet(
            frozenset({Scope.ORDER_INFO}) if with_scope else frozenset()
        ),
        access_token="secret-access",
        shop_cipher="secret-cipher",
    )


async def order_factory():
    engine, factory = create_engine_and_session_factory(
        DatabaseSettings(url="sqlite+aiosqlite:///:memory:", path=None)
    )
    await migrate_engine(engine)
    async with factory.begin() as session:
        binding = ShopBinding(
            open_id="order-owner",
            shop_id="order-shop",
            region="MY",
            listing_mode=ListingMode.UNKNOWN.value,
            authorization_status=AuthorizationStatus.ACTIVE.value,
        )
        session.add(binding)
        await session.flush()
    return engine, factory, binding


def test_order_normalization_drops_buyer_and_recipient_data() -> None:
    order = normalize_order(
        {
            "id": "order-sensitive",
            "status": "UNPAID",
            "create_time": 1_772_323_200,
            "payment": {"total_amount": "12.50", "currency": "MYR"},
            "recipient_address": {
                "name": "must-not-persist",
                "phone_number": "+60123456789",
                "full_address": "private address",
            },
            "buyer_email": "private@example.com",
            "line_items": [
                {
                    "id": "line-1",
                    "seller_sku": "SKU-1",
                    "quantity": 1,
                    "sale_price": "12.50",
                }
            ],
        }
    )
    payload = normalized_order_to_payload(order)
    text = repr(payload)
    assert order.order_id == "order-sensitive"
    assert order.item_count == 1
    assert "must-not-persist" not in text
    assert "+60123456789" not in text
    assert "private@example.com" not in text


def test_order_detail_batches_enforce_platform_limit() -> None:
    batches = detail_batches([f"order-{index}" for index in range(120)])
    assert tuple(len(batch) for batch in batches) == (50, 50, 20)
    with pytest.raises(ValueError, match="unique"):
        detail_batches(["same", "same"])
    with pytest.raises(ValueError, match="between 1 and 50"):
        detail_batches(["one"], size=51)


@pytest.mark.asyncio
async def test_order_update_without_line_field_preserves_stored_lines() -> None:
    engine, factory, binding = await order_factory()
    initial = normalize_order(
        {
            "id": "order-presence",
            "status": "UNPAID",
            "line_items": [{"id": "line-existing", "quantity": 2}],
        }
    )
    summary_only = normalize_order(
        {"id": "order-presence", "status": "AWAITING_SHIPMENT"}
    )
    assert initial.lines_present is True
    assert summary_only.lines_present is False
    try:
        async with factory.begin() as session:
            await upsert_orders(
                session,
                shop_binding_id=binding.id,
                orders=(initial,),
                detail=True,
            )
        async with factory.begin() as session:
            await upsert_orders(
                session,
                shop_binding_id=binding.id,
                orders=(summary_only,),
                detail=True,
            )
        async with factory() as session:
            record = await session.scalar(select(OrderRecord))
            lines = tuple(await session.scalars(select(OrderLineRecord)))
            assert record is not None
            assert record.order_status == "AWAITING_SHIPMENT"
            assert record.item_count == 2
            assert [line.platform_line_id for line in lines] == ["line-existing"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_order_list_empty_then_missing_detail_preserves_stored_lines() -> None:
    engine, factory, binding = await order_factory()
    initial = normalize_order(
        {
            "id": "order-presence",
            "status": "UNPAID",
            "line_items": [{"id": "line-existing", "quantity": 2}],
        }
    )
    list_with_explicit_empty = normalize_order(
        {
            "id": "order-presence",
            "status": "AWAITING_SHIPMENT",
            "items": [],
        }
    )
    detail_without_lines = normalize_order(
        {"id": "order-presence", "status": "SHIPPED"}
    )
    assert list_with_explicit_empty.lines_present is True
    assert detail_without_lines.lines_present is False
    try:
        async with factory.begin() as session:
            await upsert_orders(
                session,
                shop_binding_id=binding.id,
                orders=(initial,),
                detail=True,
            )
        async with factory.begin() as session:
            await upsert_orders(
                session,
                shop_binding_id=binding.id,
                orders=(list_with_explicit_empty,),
                detail=False,
            )
        async with factory.begin() as session:
            await upsert_orders(
                session,
                shop_binding_id=binding.id,
                orders=(detail_without_lines,),
                detail=True,
            )
        async with factory() as session:
            record = await session.scalar(select(OrderRecord))
            lines = tuple(await session.scalars(select(OrderLineRecord)))
            assert record is not None
            assert record.order_status == "SHIPPED"
            assert record.item_count == 2
            assert [line.platform_line_id for line in lines] == ["line-existing"]
            assert [line.quantity for line in lines] == [2]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_order_update_with_explicit_empty_lines_clears_stored_lines() -> None:
    engine, factory, binding = await order_factory()
    initial = normalize_order(
        {
            "id": "order-presence",
            "status": "UNPAID",
            "line_items": [{"id": "line-existing", "quantity": 2}],
        }
    )
    explicit_empty = normalize_order(
        {"id": "order-presence", "status": "CANCELLED", "line_items": []}
    )
    assert explicit_empty.lines_present is True
    try:
        async with factory.begin() as session:
            await upsert_orders(
                session,
                shop_binding_id=binding.id,
                orders=(initial,),
                detail=True,
            )
        async with factory.begin() as session:
            await upsert_orders(
                session,
                shop_binding_id=binding.id,
                orders=(explicit_empty,),
                detail=True,
            )
        async with factory() as session:
            record = await session.scalar(select(OrderRecord))
            lines = tuple(await session.scalars(select(OrderLineRecord)))
            assert record is not None
            assert record.order_status == "CANCELLED"
            assert record.item_count == 0
            assert lines == ()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_order_update_with_present_lines_replaces_stored_lines() -> None:
    engine, factory, binding = await order_factory()
    initial = normalize_order(
        {
            "id": "order-presence",
            "status": "UNPAID",
            "items": [{"id": "line-old", "quantity": 1}],
        }
    )
    replacement = normalize_order(
        {
            "id": "order-presence",
            "status": "SHIPPED",
            "items": [{"id": "line-new", "quantity": 3}],
        }
    )
    assert replacement.lines_present is True
    try:
        async with factory.begin() as session:
            await upsert_orders(
                session,
                shop_binding_id=binding.id,
                orders=(initial,),
                detail=False,
            )
        async with factory.begin() as session:
            await upsert_orders(
                session,
                shop_binding_id=binding.id,
                orders=(replacement,),
                detail=False,
            )
        async with factory() as session:
            record = await session.scalar(select(OrderRecord))
            lines = tuple(await session.scalars(select(OrderLineRecord)))
            assert record is not None
            assert record.order_status == "SHIPPED"
            assert record.item_count == 3
            assert [line.platform_line_id for line in lines] == ["line-new"]
            assert [line.quantity for line in lines] == [3]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_order_gateway_uses_independent_list_and_detail_contracts() -> None:
    engine, _factory, binding = await order_factory()
    client = CapturingTikTokClient()
    gateway = TikTokOrderGateway(client)  # type: ignore[arg-type]
    try:
        access = order_context(binding)
        page = await gateway.search(access, page_size=25, page_token="cursor")
        details = await gateway.details(access, ["order-gateway"])
        assert page.orders[0].order_id == "order-gateway"
        assert page.next_page_token == "next-token"
        assert details[0].lines[0].line_id == "gateway-line"
        assert client.calls[0][0] == "orders.search"
        assert client.calls[0][1]["query"] == {"page_size": 25, "page_token": "cursor"}
        assert client.calls[1][0] == "orders.detail"
        assert client.calls[1][1]["query"] == {"ids": "order-gateway"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_order_sync_persists_details_and_completes_checkpoint() -> None:
    engine, factory, binding = await order_factory()
    gateway = FakeOrderGateway()
    service = OrderService(gateway)
    start = datetime(2026, 3, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    try:
        summary = await service.sync_window(
            factory,
            order_context(binding),
            window_start=start,
            window_end=end,
        )
        assert summary.completed and summary.pages == 2
        assert summary.listed_orders == 2 and summary.detailed_orders == 2
        assert gateway.search_tokens == [None, "page-2"]
        assert gateway.detail_batches == [("order-1",), ("order-2",)]

        async with factory() as session:
            records = tuple(
                await session.scalars(select(OrderRecord).order_by(OrderRecord.platform_order_id))
            )
            lines = tuple(await session.scalars(select(OrderLineRecord)))
            checkpoint = await session.scalar(select(OrderSyncCheckpoint))
            assert [record.platform_order_id for record in records] == ["order-1", "order-2"]
            assert all(record.item_count == 2 for record in records)
            assert [line.seller_sku for line in lines] == ["SELLER-SKU-1", "SELLER-SKU-1"]
            assert checkpoint is not None and checkpoint.page_token is None
            assert checkpoint.last_success_at is not None
            assert checkpoint.last_run_summary["completed"] is True
            assert not hasattr(records[0], "buyer_email")
            assert not hasattr(records[0], "recipient_address")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_order_sync_resumes_exact_saved_page_token() -> None:
    engine, factory, binding = await order_factory()
    gateway = FakeOrderGateway()
    service = OrderService(gateway)
    start = datetime(2026, 3, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    try:
        first = await service.sync_window(
            factory,
            order_context(binding),
            window_start=start,
            window_end=end,
            max_pages=1,
        )
        assert not first.completed and first.next_page_token == "page-2"
        second = await service.sync_window(
            factory,
            order_context(binding),
            window_start=start,
            window_end=end,
        )
        assert second.completed and second.pages == 1
        assert gateway.search_tokens == [None, "page-2"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_missing_order_scope_blocks_before_gateway() -> None:
    engine, _factory, binding = await order_factory()
    gateway = FakeOrderGateway()
    service = OrderService(gateway)
    try:
        with pytest.raises(CommerceAccessBlocked, match="seller.order.info"):
            await service.fetch_page(order_context(binding, with_scope=False))
        assert gateway.search_tokens == []
    finally:
        await engine.dispose()