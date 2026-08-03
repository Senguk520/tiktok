"""PII-minimized order facts and strict TikTok response normalization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class OrderPayloadError(ValueError):
    """Raised when an order response lacks stable business identifiers."""


@dataclass(frozen=True, slots=True)
class NormalizedOrderLine:
    line_id: str
    quantity: int
    product_id: str | None = None
    sku_id: str | None = None
    seller_sku: str | None = None
    status: str | None = None
    currency: str | None = None
    sale_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.line_id.strip():
            raise OrderPayloadError("order line id is required")
        if self.quantity <= 0:
            raise OrderPayloadError("order line quantity must be positive")
        if self.sale_price is not None and self.sale_price < 0:
            raise OrderPayloadError("order line price cannot be negative")


@dataclass(frozen=True, slots=True)
class NormalizedOrder:
    order_id: str
    status: str
    lines: tuple[NormalizedOrderLine, ...] = ()
    fulfillment_type: str | None = None
    shipping_type: str | None = None
    currency: str | None = None
    total_amount: Decimal | None = None
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.order_id.strip() or not self.status.strip():
            raise OrderPayloadError("order id and status are required")
        line_ids = [line.line_id for line in self.lines]
        if len(line_ids) != len(set(line_ids)):
            raise OrderPayloadError("order line ids must be unique")
        if self.total_amount is not None and self.total_amount < 0:
            raise OrderPayloadError("order total cannot be negative")

    @property
    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)


@dataclass(frozen=True, slots=True)
class NormalizedOrderPage:
    orders: tuple[NormalizedOrder, ...]
    next_page_token: str | None
    total_count: int | None
    request_id: str | None


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OrderPayloadError(f"{field} must be an object")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_text(raw: Mapping[str, Any], keys: Sequence[str], *, field: str) -> str:
    for key in keys:
        value = _optional_text(raw.get(key))
        if value:
            return value
    raise OrderPayloadError(f"{field} is required")


def _decimal(value: object, *, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OrderPayloadError(f"{field} is not a decimal") from exc
    if not parsed.is_finite():
        raise OrderPayloadError(f"{field} must be finite")
    return parsed


def _timestamp(value: object, *, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise OrderPayloadError(f"{field} is not a timestamp")
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            return datetime.fromtimestamp(int(value), tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise OrderPayloadError(f"{field} is outside the timestamp range") from exc
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OrderPayloadError(f"{field} is not an ISO timestamp") from exc
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    raise OrderPayloadError(f"{field} is not a timestamp")


def _line(raw: Mapping[str, Any], *, order_currency: str | None) -> NormalizedOrderLine:
    quantity = raw.get("quantity", 1)
    if not isinstance(quantity, int) or isinstance(quantity, bool):
        raise OrderPayloadError("order line quantity must be an integer")
    currency = _optional_text(raw.get("currency")) or order_currency
    return NormalizedOrderLine(
        line_id=_required_text(raw, ("id", "line_item_id", "order_line_id"), field="line id"),
        product_id=_optional_text(raw.get("product_id")),
        sku_id=_optional_text(raw.get("sku_id")),
        seller_sku=_optional_text(raw.get("seller_sku")),
        status=_optional_text(raw.get("display_status") or raw.get("status")),
        quantity=quantity,
        currency=currency.upper() if currency else None,
        sale_price=_decimal(
            raw.get("sale_price", raw.get("sku_sale_price")),
            field="line sale price",
        ),
    )


def normalize_order(raw: Mapping[str, Any]) -> NormalizedOrder:
    """Keep operational facts while deliberately dropping recipient and buyer fields."""

    payment_value = raw.get("payment")
    payment = payment_value if isinstance(payment_value, Mapping) else {}
    currency = _optional_text(payment.get("currency") or raw.get("currency"))
    line_values = raw.get("line_items", raw.get("items", []))
    if not isinstance(line_values, Sequence) or isinstance(
        line_values, (str, bytes, bytearray)
    ):
        raise OrderPayloadError("order line_items must be an array")
    lines = tuple(
        _line(_mapping(item, field=f"line_items[{index}]"), order_currency=currency)
        for index, item in enumerate(line_values)
    )
    return NormalizedOrder(
        order_id=_required_text(raw, ("id", "order_id"), field="order id"),
        status=_required_text(raw, ("status", "order_status"), field="order status").upper(),
        lines=lines,
        fulfillment_type=_optional_text(raw.get("fulfillment_type")),
        shipping_type=_optional_text(raw.get("shipping_type")),
        currency=currency.upper() if currency else None,
        total_amount=_decimal(
            payment.get("total_amount", raw.get("total_amount")),
            field="order total amount",
        ),
        source_created_at=_timestamp(
            raw.get("create_time", raw.get("created_at")),
            field="order create time",
        ),
        source_updated_at=_timestamp(
            raw.get("update_time", raw.get("updated_at")),
            field="order update time",
        ),
    )


def normalized_order_to_payload(order: NormalizedOrder) -> dict[str, Any]:
    """Return the exact PII-free representation used for persistence hashes."""

    return {
        "order_id": order.order_id,
        "status": order.status,
        "fulfillment_type": order.fulfillment_type,
        "shipping_type": order.shipping_type,
        "currency": order.currency,
        "total_amount": (
            format(order.total_amount, "f") if order.total_amount is not None else None
        ),
        "source_created_at": (
            order.source_created_at.isoformat() if order.source_created_at else None
        ),
        "source_updated_at": (
            order.source_updated_at.isoformat() if order.source_updated_at else None
        ),
        "lines": [
            {
                "line_id": line.line_id,
                "product_id": line.product_id,
                "sku_id": line.sku_id,
                "seller_sku": line.seller_sku,
                "status": line.status,
                "quantity": line.quantity,
                "currency": line.currency,
                "sale_price": (
                    format(line.sale_price, "f") if line.sale_price is not None else None
                ),
            }
            for line in order.lines
        ],
    }