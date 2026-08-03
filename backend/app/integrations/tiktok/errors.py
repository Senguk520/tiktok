"""TikTok error classification without retaining sensitive upstream bodies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum


class ErrorCategory(StrEnum):
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    AUTHORIZATION = "AUTHORIZATION"
    SCOPE = "SCOPE"
    VALIDATION = "VALIDATION"
    UPSTREAM = "UPSTREAM"
    AMBIGUOUS_WRITE = "AMBIGUOUS_WRITE"


@dataclass(frozen=True, slots=True)
class Failure:
    category: ErrorCategory
    http_status: int | None
    business_code: int | str | None
    retry_at: datetime | None = None


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> datetime | None:
    if value is None or not value.strip():
        return None
    current = datetime.now(UTC) if now is None else now
    text = value.strip()
    if text.isdigit():
        return current + timedelta(seconds=int(text))
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def classify_failure(
    *,
    http_status: int | None,
    business_code: int | str | None = None,
    retry_after: str | None = None,
    now: datetime | None = None,
    ambiguous_write: bool = False,
) -> Failure:
    if ambiguous_write:
        return Failure(ErrorCategory.AMBIGUOUS_WRITE, http_status, business_code)
    code_text = "" if business_code is None else str(business_code)
    retry_at = parse_retry_after(retry_after, now=now)
    if http_status == 429 or code_text == "36009002":
        return Failure(ErrorCategory.RATE_LIMITED, http_status, business_code, retry_at)
    if http_status == 503:
        return Failure(ErrorCategory.SERVICE_UNAVAILABLE, http_status, business_code, retry_at)
    if http_status in {401, 403}:
        return Failure(ErrorCategory.AUTHORIZATION, http_status, business_code)
    if http_status is not None and 400 <= http_status < 500:
        return Failure(ErrorCategory.VALIDATION, http_status, business_code)
    return Failure(ErrorCategory.UPSTREAM, http_status, business_code, retry_at)