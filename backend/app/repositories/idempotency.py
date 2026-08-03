"""Durable idempotency registration and lease-based operation claiming."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IdempotentOperation
from app.domain.enums import WriteState


class IdempotencyConflict(ValueError):
    """A client/business key was reused for a different payload."""


@dataclass(frozen=True, slots=True)
class IdempotencyRequest:
    shop_binding_id: str
    operation: str
    business_key: str
    payload_hash: str
    idempotency_key: str


def canonical_payload_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest_client_key(value: str) -> str:
    if not value or len(value) > 255:
        raise ValueError("idempotency key must contain 1-255 characters")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def register_operation(
    session: AsyncSession,
    request: IdempotencyRequest,
) -> tuple[IdempotentOperation, bool]:
    """Register once; return ``(operation, created)`` or reject key drift."""

    client_key_hash = _digest_client_key(request.idempotency_key)
    by_client_key = await session.scalar(
        select(IdempotentOperation).where(
            IdempotentOperation.shop_binding_id == request.shop_binding_id,
            IdempotentOperation.idempotency_key_hash == client_key_hash,
        )
    )
    if by_client_key is not None:
        if (
            by_client_key.operation != request.operation
            or by_client_key.business_key != request.business_key
            or by_client_key.payload_hash != request.payload_hash
        ):
            raise IdempotencyConflict("idempotency key was reused with different intent")
        return by_client_key, False

    by_business_key = await session.scalar(
        select(IdempotentOperation).where(
            IdempotentOperation.shop_binding_id == request.shop_binding_id,
            IdempotentOperation.operation == request.operation,
            IdempotentOperation.business_key == request.business_key,
        )
    )
    if by_business_key is not None:
        if by_business_key.payload_hash != request.payload_hash:
            raise IdempotencyConflict("business key already has a different payload")
        return by_business_key, False

    operation = IdempotentOperation(
        shop_binding_id=request.shop_binding_id,
        operation=request.operation,
        business_key=request.business_key,
        payload_hash=request.payload_hash,
        idempotency_key_hash=client_key_hash,
        state=WriteState.VALIDATING.value,
    )
    session.add(operation)
    await session.flush()
    return operation, True


async def claim_due_operations(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 20,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> tuple[IdempotentOperation, ...]:
    """Atomically lease QUEUED operations; expired leases are recoverable."""

    if not worker_id or limit <= 0 or lease_seconds <= 0:
        raise ValueError("valid worker, limit and lease are required")
    current = datetime.now(UTC) if now is None else now
    due = and_(
        IdempotentOperation.state == WriteState.QUEUED.value,
        or_(
            IdempotentOperation.next_attempt_at.is_(None),
            IdempotentOperation.next_attempt_at <= current,
        ),
        or_(
            IdempotentOperation.lease_until.is_(None),
            IdempotentOperation.lease_until <= current,
        ),
    )
    candidate_ids = tuple(
        await session.scalars(
            select(IdempotentOperation.id)
            .where(due)
            .order_by(IdempotentOperation.created_at, IdempotentOperation.id)
            .limit(limit)
        )
    )
    claimed_ids: list[str] = []
    lease_until = current + timedelta(seconds=lease_seconds)
    for operation_id in candidate_ids:
        result = await session.execute(
            update(IdempotentOperation)
            .where(IdempotentOperation.id == operation_id, due)
            .values(
                lease_owner=worker_id,
                lease_until=lease_until,
                attempts=IdempotentOperation.attempts + 1,
                updated_at=current,
            )
        )
        if result.rowcount == 1:
            claimed_ids.append(operation_id)
    if not claimed_ids:
        return ()
    rows = await session.scalars(
        select(IdempotentOperation)
        .where(IdempotentOperation.id.in_(claimed_ids))
        .order_by(IdempotentOperation.created_at, IdempotentOperation.id)
    )
    return tuple(rows)


async def release_operation_lease(
    session: AsyncSession,
    operation_id: str,
    *,
    worker_id: str,
    next_state: WriteState,
    retry_at: datetime | None = None,
) -> bool:
    result = await session.execute(
        update(IdempotentOperation)
        .where(
            IdempotentOperation.id == operation_id,
            IdempotentOperation.lease_owner == worker_id,
        )
        .values(
            state=next_state.value,
            lease_owner=None,
            lease_until=None,
            next_attempt_at=retry_at,
            updated_at=datetime.now(UTC),
        )
    )
    return result.rowcount == 1