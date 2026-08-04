"""The sole Core-to-Collector HTTP client for the fixed loopback boundary."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from shared.collector_contract import (
    CollectorContractError,
    CollectorImportEnvelopeV1,
    CollectorImportReceiptV1,
)
from shared.security import (
    INTERNAL_HMAC_SIGNATURE_HEADER,
    INTERNAL_HMAC_TIMESTAMP_HEADER,
    SecurityConfigurationError,
    load_internal_hmac_secret_from_env,
    sign_internal_message,
    utc_timestamp,
)

COLLECTOR_BASE_URL = "http://127.0.0.1:8010"
_RESOURCE_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_JSON_LIMIT = 2 * 1024 * 1024
_IMAGE_LIMIT = 5 * 1024 * 1024


class CollectorClientError(RuntimeError):
    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class CollectorClientSettings:
    secret: bytes = field(repr=False)
    connect_timeout_seconds: float = 2.0
    read_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.secret:
            raise SecurityConfigurationError("internal HMAC secret must not be empty")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("Collector timeouts must be positive")

    @classmethod
    def from_env(cls) -> CollectorClientSettings:
        return cls(secret=load_internal_hmac_secret_from_env())


class _StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CollectorJobCreated(_StrictResponse):
    job_id: str
    source: str
    source_mode: str
    status: str
    reused: bool


class CollectorJobStatus(_StrictResponse):
    job_id: str
    source: str
    source_mode: str
    status: str
    attempts: int
    max_attempts: int
    result_id: str | None
    imported: bool
    error_code: str | None


class CollectorReceiptAcknowledgement(_StrictResponse):
    result_id: str
    imported: bool
    newly_marked: bool


@dataclass(frozen=True, slots=True)
class CollectorImage:
    content_type: str
    content: bytes


class CollectorHttpClient:
    """Create a new proxy-free httpx client for every signed request."""

    def __init__(
        self,
        settings: CollectorClientSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def create_job(
        self,
        *,
        source: str,
        source_mode: str,
        source_url: str,
    ) -> CollectorJobCreated:
        document = await self._json_request(
            "POST",
            "/internal/v1/jobs",
            payload={
                "source": source,
                "source_mode": source_mode,
                "source_url": source_url,
            },
        )
        try:
            return CollectorJobCreated.model_validate(document)
        except ValidationError as exc:
            raise CollectorClientError("COLLECTOR_RESPONSE_INVALID") from exc

    async def get_job(self, job_id: str) -> CollectorJobStatus:
        selected = _resource_id(job_id)
        document = await self._json_request(
            "GET",
            f"/internal/v1/jobs/{selected}",
        )
        try:
            return CollectorJobStatus.model_validate(document)
        except ValidationError as exc:
            raise CollectorClientError("COLLECTOR_RESPONSE_INVALID") from exc

    async def export_result(self, result_id: str) -> CollectorImportEnvelopeV1:
        selected = _resource_id(result_id)
        document = await self._json_request(
            "GET",
            f"/internal/v1/results/{selected}",
        )
        try:
            return CollectorImportEnvelopeV1.from_mapping(document)
        except CollectorContractError as exc:
            raise CollectorClientError("COLLECTOR_RESPONSE_INVALID") from exc

    async def acknowledge_receipt(
        self,
        receipt: CollectorImportReceiptV1,
    ) -> CollectorReceiptAcknowledgement:
        selected = _resource_id(receipt.result_id)
        document = await self._json_request(
            "POST",
            f"/internal/v1/results/{selected}/receipt",
            payload=receipt.to_mapping(),
        )
        try:
            acknowledgement = CollectorReceiptAcknowledgement.model_validate(document)
        except ValidationError as exc:
            raise CollectorClientError("COLLECTOR_RESPONSE_INVALID") from exc
        if acknowledgement.result_id != receipt.result_id or not acknowledgement.imported:
            raise CollectorClientError("COLLECTOR_RESPONSE_INVALID")
        return acknowledgement

    async def read_image(self, image_record_id: str) -> CollectorImage:
        selected = _resource_id(image_record_id)
        response = await self._request(
            "GET",
            f"/internal/v1/images/{selected}",
            maximum=_IMAGE_LIMIT,
        )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            raise CollectorClientError("COLLECTOR_RESPONSE_INVALID")
        return CollectorImage(content_type=content_type, content=response.content)

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = b"" if payload is None else _json_bytes(payload)
        response = await self._request(
            method,
            path,
            body=body,
            content_type="application/json" if payload is not None else None,
            maximum=_JSON_LIMIT,
        )
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise CollectorClientError("COLLECTOR_RESPONSE_INVALID")
        try:
            document = json.loads(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CollectorClientError("COLLECTOR_RESPONSE_INVALID") from exc
        if not isinstance(document, dict) or any(not isinstance(key, str) for key in document):
            raise CollectorClientError("COLLECTOR_RESPONSE_INVALID")
        return document

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        content_type: str | None = None,
        maximum: int,
    ) -> httpx.Response:
        timestamp = utc_timestamp()
        signature = sign_internal_message(
            self._settings.secret,
            timestamp=timestamp,
            method=method,
            path=path,
            body=body,
        )
        headers = {
            "Accept": "application/json",
            INTERNAL_HMAC_TIMESTAMP_HEADER: str(timestamp),
            INTERNAL_HMAC_SIGNATURE_HEADER: signature,
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        timeout = httpx.Timeout(
            connect=self._settings.connect_timeout_seconds,
            read=self._settings.read_timeout_seconds,
            write=self._settings.read_timeout_seconds,
            pool=self._settings.connect_timeout_seconds,
        )
        try:
            async with httpx.AsyncClient(
                base_url=COLLECTOR_BASE_URL,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                http2=False,
                transport=self._transport,
            ) as client:
                async with client.stream(method, path, content=body, headers=headers) as streamed:
                    content = await _read_bounded(streamed, maximum)
                    response = httpx.Response(
                        status_code=streamed.status_code,
                        headers=streamed.headers,
                        content=content,
                        request=streamed.request,
                    )
        except CollectorClientError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise CollectorClientError("COLLECTOR_UNAVAILABLE") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise _remote_failure(response)
        return response


def _resource_id(value: str) -> str:
    selected = value.strip()
    if not _RESOURCE_ID.fullmatch(selected):
        raise CollectorClientError("COLLECTOR_RESOURCE_ID_INVALID")
    return selected


def _json_bytes(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CollectorClientError("COLLECTOR_REQUEST_INVALID") from exc


async def _read_bounded(response: httpx.Response, maximum: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > maximum:
                raise CollectorClientError("COLLECTOR_RESPONSE_TOO_LARGE")
        except ValueError as exc:
            raise CollectorClientError("COLLECTOR_RESPONSE_INVALID") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > maximum:
            raise CollectorClientError("COLLECTOR_RESPONSE_TOO_LARGE")
        chunks.append(chunk)
    return b"".join(chunks)


def _remote_failure(response: httpx.Response) -> CollectorClientError:
    remote_code = "COLLECTOR_REQUEST_FAILED"
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type == "application/json":
        try:
            document = json.loads(response.content)
            candidate = document.get("error", {}).get("code")
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            candidate = None
        if isinstance(candidate, str) and _ERROR_CODE.fullmatch(candidate):
            remote_code = candidate
    return CollectorClientError(remote_code, status_code=response.status_code)


__all__ = [
    "COLLECTOR_BASE_URL",
    "CollectorClientError",
    "CollectorClientSettings",
    "CollectorHttpClient",
    "CollectorImage",
    "CollectorJobCreated",
    "CollectorJobStatus",
    "CollectorReceiptAcknowledgement",
]