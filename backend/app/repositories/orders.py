"""PII-minimized order persistence and resumable sync checkpoints."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrderLineRecord, OrderRecord, OrderSyncCheckpoint
from app.domain.orders import NormalizedOrder, normalized_order_to_payload
from app.repositories.idempotency import canonical_payload_hash


def _amount(value: object) -> str | None:
    return None if value is None else format(value, "f")


async def _replace_lines(
    session: AsyncSession,
    *,
    order_record: OrderRecord,
    order: NormalizedOrder,
) -> None:
    await session.execute(
        delete(OrderLineRecord).where(OrderLineRecord.order_record_id == order_record.id)
    )
    session.add_all(
        OrderLineRecord(
            order_record_id=order_record.id,
            platform_line_id=line.line_id,
            product_id=line.product_id,
            sku_id=line.sku_id,
            seller_sku=line.seller_sku,
            line_status=line.status,
            quantity=line.quantity,
            currency=line.currency,
            sale_price=_amount(line.sale_price),
        )
        for line in order.lines
    )


async def upsert_orders(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    orders: Sequence[NormalizedOrder],
    detail: bool,
    seen_at: datetime | None = None,
) -> tuple[OrderRecord, ...]:
    """Persist operational facts only; buyer and recipient payloads never enter SQLite."""

    current = datetime.now(UTC) if seen_at is None else seen_at
    stored: list[OrderRecord] = []
    for order in orders:
        record = await session.scalar(
            select(OrderRecord).where(
                OrderRecord.shop_binding_id == shop_binding_id,
                OrderRecord.platform_order_id == order.order_id,
            )
        )
        incoming_hash = canonical_payload_hash(normalized_order_to_payload(order))
        if record is None:
            record = OrderRecord(
                shop_binding_id=shop_binding_id,
                platform_order_id=order.order_id,
                order_status=order.status,
                item_count=order.item_count,
                normalized_hash=incoming_hash,
            )
            session.add(record)
            await session.flush()
        record.order_status = order.status
        record.fulfillment_type = order.fulfillment_type or record.fulfillment_type
        record.shipping_type = order.shipping_type or record.shipping_type
        record.currency = order.currency or record.currency
        record.total_amount = _amount(order.total_amount) or record.total_amount
        record.source_created_at = order.source_created_at or record.source_created_at
        record.source_updated_at = order.source_updated_at or record.source_updated_at
        record.last_seen_at = current
        record.normalized_hash = incoming_hash
        if detail:
            record.detail_synced_at = current
        if order.lines_present and (detail or bool(order.lines)):
            # Detail payloads may explicitly clear lines. List payloads may
            # update non-empty summaries, but an empty list never erases
            # previously persisted full details.
            record.item_count = order.item_count
            await _replace_lines(session, order_record=record, order=order)
        stored.append(record)
    await session.flush()
    return tuple(stored)


async def get_sync_checkpoint(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    stream_name: str = "orders",
) -> OrderSyncCheckpoint | None:
    return await session.scalar(
        select(OrderSyncCheckpoint).where(
            OrderSyncCheckpoint.shop_binding_id == shop_binding_id,
            OrderSyncCheckpoint.stream_name == stream_name,
        )
    )


async def save_sync_checkpoint(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    page_token: str | None,
    window_start: datetime | None,
    window_end: datetime | None,
    summary: dict[str, object],
    completed: bool,
    stream_name: str = "orders",
    now: datetime | None = None,
) -> OrderSyncCheckpoint:
    if window_start and window_end and window_start > window_end:
        raise ValueError("order sync window start cannot be after its end")
    checkpoint = await get_sync_checkpoint(
        session,
        shop_binding_id=shop_binding_id,
        stream_name=stream_name,
    )
    if checkpoint is None:
        checkpoint = OrderSyncCheckpoint(
            shop_binding_id=shop_binding_id,
            stream_name=stream_name,
        )
        session.add(checkpoint)
    checkpoint.page_token = None if completed else page_token
    checkpoint.window_start = window_start
    checkpoint.window_end = window_end
    checkpoint.last_run_summary = summary
    if completed:
        checkpoint.last_success_at = datetime.now(UTC) if now is None else now
    await session.flush()
    return checkpoint


async def local_order_details(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    order_ids: Sequence[str],
) -> tuple[tuple[OrderRecord, tuple[OrderLineRecord, ...]], ...]:
    cleaned = tuple(value.strip() for value in order_ids if value.strip())
    if not cleaned or len(cleaned) > 50 or len(set(cleaned)) != len(cleaned):
        raise ValueError("local order lookup requires 1-50 unique ids")
    records = tuple(
        await session.scalars(
            select(OrderRecord)
            .where(
                OrderRecord.shop_binding_id == shop_binding_id,
                OrderRecord.platform_order_id.in_(cleaned),
            )
            .order_by(OrderRecord.source_created_at.desc(), OrderRecord.platform_order_id)
        )
    )
    results: list[tuple[OrderRecord, tuple[OrderLineRecord, ...]]] = []
    for record in records:
        lines = tuple(
            await session.scalars(
                select(OrderLineRecord)
                .where(OrderLineRecord.order_record_id == record.id)
                .order_by(OrderLineRecord.platform_line_id)
            )
        )
        results.append((record, lines))
    return tuple(results)