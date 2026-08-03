"""Single redaction policy for logs, audit records, and error payloads.

Redaction is intentionally lossy.  These helpers should be applied before a
value reaches a logger or a persisted audit record; callers must not retain an
unredacted copy merely to display an error later.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
REDACTED_BODY = "[REDACTED_BODY]"

_SENSITIVE_KEY_PARTS = frozenset(
    {
        "access_token",
        "accesstoken",
        "app_secret",
        "appsecret",
        "authorization",
        "auth_code",
        "authcode",
        "code",
        "cookie",
        "cookies",
        "csrf",
        "password",
        "refresh_token",
        "refreshtoken",
        "secret",
        "session",
        "session_id",
        "sessionid",
        "shop_cipher",
        "shopcipher",
        "sign",
        "signature",
        "state",
        "token",
        "webhook_secret",
        "webhooksecret",
    }
)
_QUERY_SENSITIVE_KEYS = _SENSITIVE_KEY_PARTS | {"key", "client_secret", "clientsecret"}
_BUYER_KEY_PARTS = frozenset(
    {
        "address",
        "buyer",
        "buyer_name",
        "buyer_phone",
        "email",
        "full_name",
        "name",
        "phone",
        "postal_code",
        "postcode",
        "recipient",
        "shipping_address",
    }
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{6,}\d)(?!\d)")


def _normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_sensitive_key(key: object) -> bool:
    normalized = _normalized_key(key)
    if normalized in _SENSITIVE_KEY_PARTS:
        return True
    return any(
        normalized.endswith(part.replace("_", ""))
        for part in _SENSITIVE_KEY_PARTS
        if len(part.replace("_", "")) >= 6
    )


def is_buyer_pii_key(key: object) -> bool:
    normalized = _normalized_key(key)
    if normalized in {_normalized_key(item) for item in _BUYER_KEY_PARTS}:
        return True
    return any(
        normalized.endswith(_normalized_key(part))
        for part in _BUYER_KEY_PARTS
        if len(_normalized_key(part)) >= 5
    )


def redact_secret(value: object, *, visible_prefix: int = 0, visible_suffix: int = 0) -> str:
    """Return a bounded marker; by default no secret characters are retained."""

    if value is None:
        return REDACTED
    text = str(value)
    if not text:
        return REDACTED
    if visible_prefix < 0 or visible_suffix < 0:
        raise ValueError("visible secret lengths cannot be negative")
    if visible_prefix + visible_suffix >= len(text):
        return REDACTED
    if visible_prefix == 0 and visible_suffix == 0:
        return REDACTED
    prefix = text[:visible_prefix]
    suffix = text[-visible_suffix:] if visible_suffix else ""
    return f"{prefix}{REDACTED}{suffix}"


def redact_token(value: object) -> str:
    return redact_secret(value)


def redact_cookie_header(value: object) -> str:
    """Remove cookie values while preserving names for diagnostics."""

    if value is None:
        return REDACTED
    text = str(value)
    pairs: list[str] = []
    for item in text.split(";"):
        name, separator, _cookie_value = item.strip().partition("=")
        if not separator:
            if name:
                pairs.append(f"{name}={REDACTED}")
            continue
        pairs.append(f"{name}={REDACTED}")
    return "; ".join(pairs) if pairs else REDACTED


def _redact_string(value: str) -> str:
    # Strings without a sensitive key may still contain obvious PII in an
    # upstream error message.  Keep shape, not the original identity.
    value = _EMAIL_RE.sub(REDACTED, value)
    return _PHONE_RE.sub(REDACTED, value)


def redact_buyer_pii(value: object) -> object:
    """Recursively remove buyer identity fields and obvious inline PII."""

    if is_dataclass(value) and not isinstance(value, type):
        return redact_buyer_pii(asdict(value))
    if isinstance(value, Mapping):
        return {
            key: REDACTED if is_buyer_pii_key(key) else redact_buyer_pii(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_buyer_pii(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_buyer_pii(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def redact_value(value: object, *, key: object | None = None, buyer_pii: bool = True) -> object:
    """Recursively redact secrets first, then buyer identity fields."""

    if key is not None and is_sensitive_key(key):
        return REDACTED
    if buyer_pii and key is not None and is_buyer_pii_key(key):
        return REDACTED
    if is_dataclass(value) and not isinstance(value, type):
        return redact_value(asdict(value), buyer_pii=buyer_pii)
    if isinstance(value, Mapping):
        return {
            item_key: redact_value(item, key=item_key, buyer_pii=buyer_pii)
            for item_key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_value(item, buyer_pii=buyer_pii) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item, buyer_pii=buyer_pii) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def redact_mapping(value: Mapping[object, object], *, buyer_pii: bool = True) -> dict[object, object]:
    """Redact a mapping without mutating the original object."""

    return redact_value(value, buyer_pii=buyer_pii)  # type: ignore[return-value]


def redact_url(url: str) -> str:
    """Redact credentials and sensitive query parameters in a URL."""

    if not isinstance(url, str):
        return REDACTED
    try:
        parsed = urlsplit(url)
    except ValueError:
        return REDACTED
    netloc = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username is not None or parsed.password is not None:
        # Never preserve HTTP basic-auth material.
        netloc = f"{netloc}" if netloc else REDACTED
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if _normalized_key(key) in {_normalized_key(item) for item in _QUERY_SENSITIVE_KEYS}:
            query_pairs.append((key, REDACTED))
        else:
            query_pairs.append((key, _redact_string(value)))
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query_pairs), ""))


def redact_query_url(url: str) -> str:
    return redact_url(url)


def redact_signature_body(_body: bytes | str | object) -> str:
    """Never expose the body that was used to compute an upstream signature."""

    return REDACTED_BODY


def redact_json(value: object, *, buyer_pii: bool = True) -> str:
    """Serialize a redacted value for structured logs."""

    redacted = redact_value(value, buyer_pii=buyer_pii)
    return json.dumps(redacted, ensure_ascii=False, separators=(",", ":"), default=str)


# Compatibility names used by middleware and integration boundaries.
redact = redact_value
redact_sensitive = redact_value
redact_query = redact_query_url