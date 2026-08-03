"""Exact TikTok Shop request-signing primitives."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

QueryValue: TypeAlias = str | int | float | bool
QueryInput: TypeAlias = Mapping[str, QueryValue] | Iterable[tuple[str, QueryValue]]


class SigningError(ValueError):
    pass


def validate_timestamp(timestamp: int | str) -> int:
    text = str(timestamp)
    if not text.isdigit() or len(text) != 10:
        raise SigningError("TikTok timestamp must be 10 Unix-second digits")
    return int(text)


def _query_items(query: QueryInput) -> Sequence[tuple[str, str]]:
    source = query.items() if isinstance(query, Mapping) else query
    normalized: list[tuple[str, str]] = []
    for key, value in source:
        key_text = str(key)
        if key_text in {"sign", "access_token"}:
            continue
        if not key_text:
            raise SigningError("query key cannot be empty")
        if value is None:
            raise SigningError(f"query value cannot be null: {key_text}")
        value_text = str(value).lower() if isinstance(value, bool) else str(value)
        normalized.append((key_text, value_text))
    return tuple(sorted(normalized, key=lambda item: item[0]))


def signature_base(
    path: str,
    query: QueryInput,
    *,
    body: bytes = b"",
    content_type: str | None = "application/json",
) -> bytes:
    """Build path + sorted key/value pairs + final body bytes.

    Multipart bodies are excluded because boundaries are generated at send
    time. Every other content type signs the exact bytes passed to the client.
    """

    if not path.startswith("/") or "?" in path:
        raise SigningError("signing path must be an absolute path without query")
    query_text = "".join(f"{key}{value}" for key, value in _query_items(query))
    base = path.encode("utf-8") + query_text.encode("utf-8")
    is_multipart = (content_type or "").split(";", 1)[0].strip().lower() == "multipart/form-data"
    return base if is_multipart else base + bytes(body)


def sign_request(
    app_secret: str,
    path: str,
    query: QueryInput,
    *,
    body: bytes = b"",
    content_type: str | None = "application/json",
) -> str:
    if not app_secret:
        raise SigningError("app secret is required")
    secret = app_secret.encode("utf-8")
    base = signature_base(path, query, body=body, content_type=content_type)
    message = secret + base + secret
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class SignedQuery:
    timestamp: int
    values: Mapping[str, QueryValue]


def with_signature(
    *,
    app_key: str,
    app_secret: str,
    timestamp: int | str,
    path: str,
    query: Mapping[str, QueryValue] | None = None,
    body: bytes = b"",
    content_type: str | None = "application/json",
) -> SignedQuery:
    numeric_timestamp = validate_timestamp(timestamp)
    values: dict[str, QueryValue] = dict(query or {})
    values["app_key"] = app_key
    values["timestamp"] = numeric_timestamp
    values.pop("access_token", None)
    values.pop("sign", None)
    values["sign"] = sign_request(
        app_secret,
        path,
        values,
        body=body,
        content_type=content_type,
    )
    return SignedQuery(timestamp=numeric_timestamp, values=values)