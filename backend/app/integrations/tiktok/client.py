"""Short-lived signed TikTok HTTP client with conservative retry semantics."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.integrations.tiktok.endpoints import Endpoint, ProductImageUseCase, RetryPolicy
from app.integrations.tiktok.endpoints import endpoint as get_endpoint
from app.integrations.tiktok.errors import ErrorCategory, Failure, classify_failure
from app.integrations.tiktok.signing import QueryValue, with_signature

Reconcile = Callable[[], Awaitable[dict[str, Any] | None]]


class TikTokClientError(RuntimeError):
    def __init__(self, failure: Failure, *, endpoint_key: str) -> None:
        super().__init__(f"TikTok request failed: {endpoint_key} / {failure.category.value}")
        self.failure = failure
        self.endpoint_key = endpoint_key


@dataclass(frozen=True, slots=True)
class TikTokConfig:
    app_key: str
    app_secret: str
    api_base_url: str = "https://open-api.tiktokglobalshop.com"
    timeout_seconds: float = 20.0
    max_attempts: int = 3
    max_response_bytes: int = 5 * 1024 * 1024

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TikTokConfig:
        values = os.environ if env is None else env
        app_key = values.get("TIKTOK_APP_KEY", "").strip()
        app_secret = values.get("TIKTOK_APP_SECRET", "").strip()
        if not app_key or not app_secret:
            raise ValueError("TIKTOK_APP_KEY and TIKTOK_APP_SECRET are required")
        base = values.get("TIKTOK_API_BASE_URL", cls.api_base_url).rstrip("/")
        if not base.startswith("https://"):
            raise ValueError("TikTok API base URL must use HTTPS")
        return cls(app_key=app_key, app_secret=app_secret, api_base_url=base)


@dataclass(frozen=True, slots=True)
class TikTokResult:
    data: Any
    request_id: str | None


def retry_delay_seconds(
    attempt: int,
    *,
    base_seconds: float = 0.5,
    cap_seconds: float = 30.0,
    jitter_fraction: float = 0.0,
) -> float:
    if attempt < 1 or base_seconds <= 0 or cap_seconds <= 0:
        raise ValueError("invalid retry delay parameters")
    if not 0 <= jitter_fraction <= 1:
        raise ValueError("jitter fraction must be between zero and one")
    raw = min(base_seconds * (2 ** (attempt - 1)), cap_seconds)
    return raw * (1 - 0.25 * jitter_fraction)


def should_retry(
    selected: Endpoint,
    failure: Failure,
    *,
    attempt: int,
    max_attempts: int,
    idempotency_registered: bool,
    reconciliation_available: bool,
) -> bool:
    if attempt >= max_attempts or not selected.enabled or not selected.verified:
        return False
    if failure.category not in {
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.SERVICE_UNAVAILABLE,
        ErrorCategory.UPSTREAM,
        ErrorCategory.AMBIGUOUS_WRITE,
    }:
        return False
    if selected.retry is RetryPolicy.SAFE_READ:
        return not selected.write
    if selected.retry is RetryPolicy.IDEMPOTENT_WRITE:
        return selected.write and idempotency_registered
    if selected.retry is RetryPolicy.RECONCILE_THEN_RETRY:
        return selected.write and idempotency_registered and reconciliation_available
    return False


class TikTokClient:
    def __init__(self, config: TikTokConfig) -> None:
        self._config = config

    async def request(
        self,
        endpoint_key: str,
        *,
        access_token: str,
        path_parameters: Mapping[str, str] | None = None,
        query: Mapping[str, QueryValue] | None = None,
        shop_cipher: str | None = None,
        json_body: Mapping[str, Any] | None = None,
        idempotency_registered: bool = False,
        reconcile: Reconcile | None = None,
    ) -> TikTokResult:
        selected = get_endpoint(endpoint_key)
        path = selected.build_path(**dict(path_parameters or {}))
        query_values: dict[str, QueryValue] = dict(query or {})
        if shop_cipher is not None:
            query_values["shop_cipher"] = shop_cipher
        body = (
            json.dumps(
                json_body,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if json_body is not None
            else b""
        )
        content_type = "application/json" if json_body is not None else None
        last_failure: Failure | None = None
        for attempt in range(1, self._config.max_attempts + 1):
            timestamp = int(datetime.now(UTC).timestamp())
            signed = with_signature(
                app_key=self._config.app_key,
                app_secret=self._config.app_secret,
                timestamp=timestamp,
                path=path,
                query=query_values,
                body=body,
                content_type=content_type,
            )
            headers = {
                "Accept": "application/json",
                "x-tts-access-token": access_token,
            }
            if content_type is not None:
                headers["Content-Type"] = content_type
            try:
                async with httpx.AsyncClient(
                    base_url=self._config.api_base_url,
                    timeout=self._config.timeout_seconds,
                    follow_redirects=False,
                ) as client:
                    response = await client.request(
                        selected.method,
                        path,
                        params=signed.values,
                        headers=headers,
                        content=body or None,
                    )
            except (httpx.TimeoutException, httpx.NetworkError):
                last_failure = classify_failure(
                    http_status=None,
                    ambiguous_write=selected.write,
                )
            else:
                if len(response.content) > self._config.max_response_bytes:
                    last_failure = classify_failure(http_status=response.status_code)
                else:
                    payload = self._decode_json(response)
                    business_code = payload.get("code") if isinstance(payload, dict) else None
                    success = response.is_success and business_code in {None, 0, "0"}
                    if success:
                        data = payload.get("data") if isinstance(payload, dict) else payload
                        request_id = (
                            str(payload.get("request_id"))
                            if isinstance(payload, dict) and payload.get("request_id")
                            else None
                        )
                        return TikTokResult(data=data, request_id=request_id)
                    last_failure = classify_failure(
                        http_status=response.status_code,
                        business_code=business_code,
                        retry_after=response.headers.get("Retry-After"),
                    )
            if selected.retry is RetryPolicy.RECONCILE_THEN_RETRY and reconcile is not None:
                reconciled = await reconcile()
                if reconciled is not None:
                    return TikTokResult(data=reconciled, request_id=None)
            assert last_failure is not None
            if not should_retry(
                selected,
                last_failure,
                attempt=attempt,
                max_attempts=self._config.max_attempts,
                idempotency_registered=idempotency_registered,
                reconciliation_available=reconcile is not None,
            ):
                raise TikTokClientError(last_failure, endpoint_key=endpoint_key)
            delay = retry_delay_seconds(
                attempt,
                jitter_fraction=secrets.randbelow(10_001) / 10_000,
            )
            if last_failure.retry_at is not None:
                retry_after = (last_failure.retry_at - datetime.now(UTC)).total_seconds()
                delay = max(delay, retry_after, 0)
            await asyncio.sleep(delay)
        raise TikTokClientError(
            last_failure or classify_failure(http_status=None),
            endpoint_key=endpoint_key,
        )

    async def upload_product_image(
        self,
        *,
        access_token: str,
        image: bytes,
        filename: str,
        content_type: str,
        use_case: ProductImageUseCase | str = ProductImageUseCase.MAIN_IMAGE,
        shop_cipher: str | None = None,
    ) -> TikTokResult:
        """Upload one validated image without signing multipart body bytes.

        httpx owns multipart boundary serialization. TikTok explicitly excludes
        multipart bodies from the signature, so only the final path and query
        values are signed. Uploads are never retried automatically because the
        platform has no caller-supplied idempotency key for this endpoint.
        """

        selected = get_endpoint("product.image.upload")
        try:
            selected_use_case = ProductImageUseCase(use_case)
        except ValueError as exc:
            raise ValueError("unsupported product image use case") from exc
        if not image or not filename.strip() or not content_type.startswith("image/"):
            raise ValueError("image bytes, filename and image content type are required")
        path = selected.build_path()
        query_values: dict[str, QueryValue] = {"use_case": selected_use_case.value}
        if shop_cipher is not None:
            query_values["shop_cipher"] = shop_cipher
        signed = with_signature(
            app_key=self._config.app_key,
            app_secret=self._config.app_secret,
            timestamp=int(datetime.now(UTC).timestamp()),
            path=path,
            query=query_values,
            body=b"",
            content_type="multipart/form-data",
        )
        headers = {
            "Accept": "application/json",
            "x-tts-access-token": access_token,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._config.api_base_url,
                timeout=self._config.timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    selected.method,
                    path,
                    params=signed.values,
                    headers=headers,
                    files={"data": (filename, image, content_type)},
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            failure = classify_failure(http_status=None, ambiguous_write=True)
            raise TikTokClientError(failure, endpoint_key=selected.key) from exc
        if len(response.content) > self._config.max_response_bytes:
            failure = classify_failure(http_status=response.status_code)
            raise TikTokClientError(failure, endpoint_key=selected.key)
        payload = self._decode_json(response)
        business_code = payload.get("code") if isinstance(payload, dict) else None
        if response.is_success and business_code in {None, 0, "0"}:
            data = payload.get("data") if isinstance(payload, dict) else payload
            request_id = (
                str(payload.get("request_id"))
                if isinstance(payload, dict) and payload.get("request_id")
                else None
            )
            return TikTokResult(data=data, request_id=request_id)
        failure = classify_failure(
            http_status=response.status_code,
            business_code=business_code,
            retry_after=response.headers.get("Retry-After"),
        )
        raise TikTokClientError(failure, endpoint_key=selected.key)

    @staticmethod
    def _decode_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError):
            return None