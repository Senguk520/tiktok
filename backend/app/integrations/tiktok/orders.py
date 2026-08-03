"""TikTok order gateway with independently versioned list and detail endpoints."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.enums import Scope
from app.domain.orders import NormalizedOrder, NormalizedOrderPage, normalize_order
from app.integrations.tiktok.client import TikTokClient
from app.use_cases.commerce_context import ShopAccessContext


class OrderGatewayError(RuntimeError):
    pass


def _rows(data: Any, *, label: str) -> tuple[Mapping[str, Any], ...]:
    source = data.get("orders") if isinstance(data, Mapping) else data
    if not isinstance(source, list) or any(not isinstance(item, Mapping) for item in source):
        raise OrderGatewayError(f"TikTok {label} response has an invalid order list")
    return tuple(item for item in source if isinstance(item, Mapping))


class TikTokOrderGateway:
    def __init__(self, client: TikTokClient) -> None:
        self._client = client

    async def search(
        self,
        context: ShopAccessContext,
        *,
        page_size: int = 20,
        page_token: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> NormalizedOrderPage:
        context.require_scopes(Scope.ORDER_INFO)
        if not 1 <= page_size <= 100:
            raise ValueError("order page_size must be between 1 and 100")
        query: dict[str, str | int] = {"page_size": page_size}
        if page_token:
            query["page_token"] = page_token
        result = await self._client.request(
            "orders.search",
            access_token=context.access_token,
            shop_cipher=context.shop_cipher,
            query=query,
            json_body=dict(filters or {}),
        )
        rows = _rows(result.data, label="order search")
        container = result.data if isinstance(result.data, Mapping) else {}
        next_token = container.get("next_page_token")
        total_count = container.get("total_count")
        if total_count is not None and (
            not isinstance(total_count, int) or isinstance(total_count, bool)
        ):
            raise OrderGatewayError("TikTok order total_count is not an integer")
        return NormalizedOrderPage(
            orders=tuple(normalize_order(item) for item in rows),
            next_page_token=str(next_token) if next_token else None,
            total_count=total_count,
            request_id=result.request_id,
        )

    async def details(
        self,
        context: ShopAccessContext,
        order_ids: Sequence[str],
    ) -> tuple[NormalizedOrder, ...]:
        context.require_scopes(Scope.ORDER_INFO)
        cleaned = tuple(value.strip() for value in order_ids if value.strip())
        if not cleaned or len(cleaned) > 50 or len(set(cleaned)) != len(cleaned):
            raise ValueError("order detail requires 1-50 unique ids")
        result = await self._client.request(
            "orders.detail",
            access_token=context.access_token,
            shop_cipher=context.shop_cipher,
            query={"ids": ",".join(cleaned)},
        )
        orders = tuple(normalize_order(item) for item in _rows(result.data, label="order detail"))
        returned = {order.order_id for order in orders}
        unexpected = returned - set(cleaned)
        if unexpected:
            raise OrderGatewayError("TikTok order detail returned unrequested ids")
        return orders