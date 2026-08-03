"""Fail-closed quota decisions based on operator-confirmed Seller Center facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class QuotaDecision(StrEnum):
    ALLOW = "ALLOW"
    QUEUE = "QUEUE"
    BLOCK_STALE = "BLOCK_STALE"
    BLOCK_UNKNOWN = "BLOCK_UNKNOWN"


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    listing_limit: int | None
    submitted_count: int
    confirmed_at: datetime
    expires_at: datetime
    source: str = "SELLER_CENTER_CONFIRMED"

    @property
    def remaining(self) -> int | None:
        if self.listing_limit is None:
            return None
        return max(self.listing_limit - self.submitted_count, 0)


def decide_listing_quota(
    snapshot: QuotaSnapshot | None,
    requested: int,
    *,
    now: datetime | None = None,
) -> QuotaDecision:
    if requested <= 0:
        raise ValueError("requested listing count must be positive")
    if snapshot is None or snapshot.listing_limit is None:
        return QuotaDecision.BLOCK_UNKNOWN
    current = datetime.now(UTC) if now is None else now
    if snapshot.expires_at <= current:
        return QuotaDecision.BLOCK_STALE
    remaining = snapshot.remaining
    if remaining is None:
        return QuotaDecision.BLOCK_UNKNOWN
    return QuotaDecision.ALLOW if remaining >= requested else QuotaDecision.QUEUE