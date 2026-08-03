"""Allowlisted, PII-free audit facts for the Core database."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TypeAlias

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog
from shared.redaction import is_buyer_pii_key, is_sensitive_key, redact_value

AuditScalar: TypeAlias = str | int | bool | None
AuditValue: TypeAlias = AuditScalar | Sequence[AuditScalar]

_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.]{2,63}$")
_CODE_TEXT = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
_ALLOWED_OUTCOMES = frozenset({"SUCCESS", "FAILED", "BLOCKED", "REJECTED"})
_ALLOWED_DETAIL_KEYS = frozenset(
    {
        "character_count",
        "code",
        "disabled_jobs",
        "item_count",
        "job_type",
        "page_count",
        "provider",
        "reason",
        "run_state",
        "schedule_kind",
        "source_language",
        "target_language",
    }
)


class AuditFactRejected(ValueError):
    pass


def sanitize_audit_details(details: Mapping[str, AuditValue] | None) -> dict[str, object]:
    if details is None:
        return {}
    if len(details) > 16:
        raise AuditFactRejected("audit detail count exceeds the allowlist boundary")
    sanitized: dict[str, object] = {}
    for raw_key, value in details.items():
        key = raw_key.strip()
        if (
            key not in _ALLOWED_DETAIL_KEYS
            or (key != "code" and is_sensitive_key(key))
            or is_buyer_pii_key(key)
        ):
            raise AuditFactRejected("audit detail key is not allowed")
        sanitized[key] = _sanitize_value(value)
    return sanitized


def _sanitize_value(value: AuditValue) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -1_000_000_000 <= value <= 1_000_000_000:
            raise AuditFactRejected("audit integer is outside the bounded range")
        return value
    if isinstance(value, str):
        if not _CODE_TEXT.fullmatch(value):
            raise AuditFactRejected("audit string must be a bounded code value")
        if redact_value(value) != value:
            raise AuditFactRejected("audit string contains personal data")
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        if len(value) > 20:
            raise AuditFactRejected("audit list exceeds the bounded range")
        return [_sanitize_value(item) for item in value]
    raise AuditFactRejected("audit value type is not allowed")


async def record_audit_fact(
    session: AsyncSession,
    *,
    event_type: str,
    outcome: str,
    actor_session_id: str | None = None,
    shop_binding_id: str | None = None,
    request_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: Mapping[str, AuditValue] | None = None,
    now: datetime | None = None,
) -> AuditLog:
    normalized_event = event_type.strip().lower()
    normalized_outcome = outcome.strip().upper()
    if not _EVENT_TYPE.fullmatch(normalized_event):
        raise AuditFactRejected("audit event type is invalid")
    if normalized_outcome not in _ALLOWED_OUTCOMES:
        raise AuditFactRejected("audit outcome is invalid")
    for name, value, maximum in (
        ("request_id", request_id, 128),
        ("resource_type", resource_type, 64),
        ("resource_id", resource_id, 128),
    ):
        if value is not None and (len(value) > maximum or not _CODE_TEXT.fullmatch(value)):
            raise AuditFactRejected(f"{name} is not a bounded identifier")
    record = AuditLog(
        actor_session_id=actor_session_id,
        shop_binding_id=shop_binding_id,
        event_type=normalized_event,
        request_id=request_id,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=normalized_outcome,
        redacted_details=sanitize_audit_details(details),
        created_at=datetime.now(UTC) if now is None else now,
    )
    session.add(record)
    await session.flush()
    return record


async def list_audit_facts(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    limit: int = 100,
    before: datetime | None = None,
) -> tuple[AuditLog, ...]:
    if not 1 <= limit <= 200:
        raise ValueError("audit page size must be between 1 and 200")
    query = select(AuditLog).where(AuditLog.shop_binding_id == shop_binding_id)
    if before is not None:
        query = query.where(AuditLog.created_at < before)
    rows = await session.scalars(
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
    )
    return tuple(rows)