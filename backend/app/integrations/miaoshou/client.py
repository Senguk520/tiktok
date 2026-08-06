"""Fail-closed, short-lived HTTP client for the optional Miaoshou provider."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from app.integrations.miaoshou.signing import prepare_signed_request

_AUTH_CODES = frozenset({"signMissing", "signExpired", "signInvalid", "appNotFound", "ipNotInWhitelist"})
_PERMISSION_CODES = frozenset({"appNoPermission"})
_RATE_LIMIT_CODES = frozenset({"accountQpsRateLimit"})
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DEFAULT_BASE_URL = "https://openapi-erp.91miaoshou.com"
_DEFAULT_TIMEOUT_SECONDS = 20.0


class MiaoshouConfigurationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MiaoshouFailureCategory(StrEnum):
    AUTHORIZATION = "AUTHORIZATION"
    PERMISSION = "PERMISSION"
    RATE_LIMITED = "RATE_LIMITED"
    VALIDATION = "VALIDATION"
    UPSTREAM = "UPSTREAM"
    INVALID_RESPONSE = "INVALID_RESPONSE"


@dataclass(frozen=True, slots=True)
class MiaoshouFailure:
    category: MiaoshouFailureCategory
    code: str | None = None


class MiaoshouClientError(RuntimeError):
    def __init__(self, failure: MiaoshouFailure) -> None:
        super().__init__(f"Miaoshou request failed: {failure.category.value}")
        self.failure = failure


@dataclass(frozen=True, slots=True)
class MiaoshouConfig:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    base_url: str = _DEFAULT_BASE_URL
    timeout_seconds: float = 20.0
    max_response_bytes: int = 5 * 1024 * 1024

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MiaoshouConfig:
        values = os.environ if env is None else env
        app_key = values.get("MIAOSHOU_APP_KEY", "").strip()
        app_secret = values.get("MIAOSHOU_APP_SECRET", "").strip()
        if not app_key or not app_secret:
            raise MiaoshouConfigurationError("BLOCKED_LIVE_CREDENTIALS")
        base_url = values.get("MIAOSHOU_BASE_URL", _DEFAULT_BASE_URL).strip().rstrip("/")
        try:
            parsed = httpx.URL(base_url)
        except httpx.InvalidURL as exc:
            raise MiaoshouConfigurationError("BLOCKED_CONFIGURATION") from exc
        if (
            parsed.scheme != "https"
            or not parsed.host
            or bool(parsed.username)
            or bool(parsed.password)
            or parsed.query
            or parsed.fragment
        ):
            raise MiaoshouConfigurationError("BLOCKED_CONFIGURATION")
        raw_timeout = values.get("MIAOSHOU_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS)).strip()
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise MiaoshouConfigurationError("BLOCKED_CONFIGURATION") from exc
        if not 1 <= timeout <= 30:
            raise MiaoshouConfigurationError("BLOCKED_CONFIGURATION")
        return cls(
            app_key=app_key,
            app_secret=app_secret,
            base_url=base_url,
            timeout_seconds=timeout,
        )


def miaoshou_enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    raw = values.get("MIAOSHOU_ENABLED", "false").strip().lower()
    if raw not in {"true", "false"}:
        raise MiaoshouConfigurationError("BLOCKED_CONFIGURATION")
    return raw == "true"


def classify_business_failure(code: Any) -> MiaoshouFailure:
    safe_code = str(code) if code is not None and _SAFE_CODE.fullmatch(str(code)) else None
    if safe_code in _AUTH_CODES:
        category = MiaoshouFailureCategory.AUTHORIZATION
    elif safe_code in _PERMISSION_CODES:
        category = MiaoshouFailureCategory.PERMISSION
    elif safe_code in _RATE_LIMIT_CODES:
        category = MiaoshouFailureCategory.RATE_LIMITED
    else:
        category = MiaoshouFailureCategory.VALIDATION
    return MiaoshouFailure(category=category, code=safe_code)


class MiaoshouClient:
    def __init__(
        self,
        config: MiaoshouConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport

    async def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        prepared = prepare_signed_request(
            app_key=self._config.app_key,
            app_secret=self._config.app_secret,
            path=path,
            timestamp=int(time.time()),
            payload=payload,
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._config.base_url,
                timeout=self._config.timeout_seconds,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "POST",
                    prepared.path,
                    headers=prepared.headers,
                    content=prepared.body,
                ) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > self._config.max_response_bytes:
                            raise MiaoshouClientError(
                                MiaoshouFailure(MiaoshouFailureCategory.INVALID_RESPONSE)
                            )
        except MiaoshouClientError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise MiaoshouClientError(MiaoshouFailure(MiaoshouFailureCategory.UPSTREAM)) from exc

        if response.status_code == 429:
            raise MiaoshouClientError(MiaoshouFailure(MiaoshouFailureCategory.RATE_LIMITED))
        if response.status_code in {401, 403}:
            raise MiaoshouClientError(MiaoshouFailure(MiaoshouFailureCategory.AUTHORIZATION))
        if response.status_code >= 500:
            raise MiaoshouClientError(MiaoshouFailure(MiaoshouFailureCategory.UPSTREAM))
        if not response.is_success:
            raise MiaoshouClientError(MiaoshouFailure(MiaoshouFailureCategory.VALIDATION))
        if not content:
            raise MiaoshouClientError(MiaoshouFailure(MiaoshouFailureCategory.INVALID_RESPONSE))
        try:
            document = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MiaoshouClientError(MiaoshouFailure(MiaoshouFailureCategory.INVALID_RESPONSE)) from exc
        if not isinstance(document, dict):
            raise MiaoshouClientError(MiaoshouFailure(MiaoshouFailureCategory.INVALID_RESPONSE))
        if document.get("result") != "success":
            raise MiaoshouClientError(classify_business_failure(document.get("code")))
        return document.get("data")