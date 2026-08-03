"""SQLite-backed rate-limit windows partitioned by app/shop/endpoint/operation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RateLimitWindow


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_at: datetime | None = None


def _window_start(now: datetime, seconds: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def consume_rate_limit(
    session: AsyncSession,
    *,
    app_key_hash: str,
    shop_id: str,
    endpoint_key: str,
    operation_type: str,
    limit_value: int,
    window_seconds: int = 1,
    cost: int = 1,
    now: datetime | None = None,
) -> RateLimitDecision:
    if not all((app_key_hash, shop_id, endpoint_key, operation_type)):
        raise ValueError("rate-limit partition fields are required")
    if limit_value <= 0 or window_seconds <= 0 or cost <= 0:
        raise ValueError("rate-limit values must be positive")
    current = datetime.now(UTC) if now is None else now
    started_at = _window_start(current, window_seconds)
    row = await session.scalar(
        select(RateLimitWindow).where(
            RateLimitWindow.app_key_hash == app_key_hash,
            RateLimitWindow.shop_id == shop_id,
            RateLimitWindow.endpoint_key == endpoint_key,
            RateLimitWindow.operation_type == operation_type,
            RateLimitWindow.window_started_at == started_at,
        )
    )
    retry_at = started_at + timedelta(seconds=window_seconds)
    if row is None:
        if cost > limit_value:
            return RateLimitDecision(False, 0, retry_at)
        row = RateLimitWindow(
            app_key_hash=app_key_hash,
            shop_id=shop_id,
            endpoint_key=endpoint_key,
            operation_type=operation_type,
            window_started_at=started_at,
            window_seconds=window_seconds,
            limit_value=limit_value,
            used=cost,
        )
        session.add(row)
        await session.flush()
        return RateLimitDecision(True, limit_value - cost)

    blocked_until = _as_utc(row.blocked_until)
    if blocked_until is not None and blocked_until > current:
        return RateLimitDecision(False, 0, blocked_until)
    result = await session.execute(
        update(RateLimitWindow)
        .where(
            RateLimitWindow.id == row.id,
            RateLimitWindow.used + cost <= limit_value,
        )
        .values(
            used=RateLimitWindow.used + cost,
            limit_value=limit_value,
            updated_at=current,
        )
    )
    if result.rowcount != 1:
        remaining = max(limit_value - row.used, 0)
        return RateLimitDecision(False, remaining, retry_at)
    return RateLimitDecision(True, max(limit_value - row.used - cost, 0))


async def record_platform_throttle(
    session: AsyncSession,
    window_id: str,
    *,
    retry_at: datetime,
) -> bool:
    """Persist a 429/36009002 Retry-After boundary without guessing a QPS."""

    result = await session.execute(
        update(RateLimitWindow)
        .where(RateLimitWindow.id == window_id)
        .values(blocked_until=retry_at, updated_at=datetime.now(UTC))
    )
    return result.rowcount == 1