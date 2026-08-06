"""Deterministic Miaoshou JCOP signing and JSON request construction."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


class MiaoshouSigningError(ValueError):
    pass


def compact_json_body(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MiaoshouSigningError("Miaoshou request body is not valid JSON") from exc


def signature_message(
    *,
    app_secret: str,
    path: str,
    timestamp: int,
    app_key: str,
    body: bytes,
) -> bytes:
    if not app_secret or not app_key:
        raise MiaoshouSigningError("Miaoshou app credentials are required")
    if not path.startswith("/") or "?" in path or "://" in path:
        raise MiaoshouSigningError("Miaoshou signing path must exclude host and query")
    if timestamp <= 0 or len(str(timestamp)) != 10:
        raise MiaoshouSigningError("Miaoshou timestamp must use Unix seconds")
    try:
        prefix = f"{app_secret}{path}{timestamp}{app_key}".encode()
        suffix = app_secret.encode()
    except UnicodeEncodeError as exc:
        raise MiaoshouSigningError("Miaoshou signing values must be UTF-8 encodable") from exc
    return prefix + bytes(body) + suffix


def sign_request(
    *,
    app_secret: str,
    path: str,
    timestamp: int,
    app_key: str,
    body: bytes,
) -> str:
    message = signature_message(
        app_secret=app_secret,
        path=path,
        timestamp=timestamp,
        app_key=app_key,
        body=body,
    )
    return hmac.new(app_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class SignedMiaoshouRequest:
    path: str
    body: bytes = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)


def prepare_signed_request(
    *,
    app_key: str,
    app_secret: str,
    path: str,
    timestamp: int,
    payload: Mapping[str, Any],
) -> SignedMiaoshouRequest:
    body = compact_json_body(payload)
    signature = sign_request(
        app_secret=app_secret,
        path=path,
        timestamp=timestamp,
        app_key=app_key,
        body=body,
    )
    return SignedMiaoshouRequest(
        path=path,
        body=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-app-key": app_key,
            "x-timestamp": str(timestamp),
            "x-sign": signature,
        },
    )