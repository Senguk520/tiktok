"""Stable, redacted API failures and per-request correlation identifiers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import format_datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.domain.orders import OrderPayloadError
from app.domain.product_payload import ProductPayloadError
from app.integrations.miaoshou.client import MiaoshouClientError, MiaoshouFailureCategory
from app.integrations.tiktok.client import TikTokClientError
from app.integrations.tiktok.errors import ErrorCategory
from app.integrations.tiktok.orders import OrderGatewayError
from app.integrations.tiktok.products import ProductGatewayError
from app.repositories.catalog import DraftConflict, DraftNotFound, ListingQuotaBlocked
from app.repositories.idempotency import IdempotencyConflict
from app.use_cases.commerce_context import CommerceAccessBlocked
from app.use_cases.listing_mode import ListingModeBlocked
from app.use_cases.products import ProductSubmissionBlocked, ProductSubmissionInProgress
from shared.security import AuthenticationError, SecurityConfigurationError


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    request_id: str


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ErrorBody


class ApiProblem(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.safe_message = message


@dataclass(frozen=True, slots=True)
class _MappedFailure:
    status_code: int
    code: str
    message: str
    headers: dict[str, str] | None = None


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and value else str(uuid4())


def _tiktok_failure(exc: TikTokClientError) -> _MappedFailure:
    category = exc.failure.category
    if category in {ErrorCategory.AUTHORIZATION, ErrorCategory.SCOPE}:
        return _MappedFailure(403, "TIKTOK_ACCESS_BLOCKED", "TikTok authorization or scope blocked the operation")
    if category is ErrorCategory.VALIDATION:
        return _MappedFailure(422, "TIKTOK_VALIDATION_REJECTED", "TikTok rejected the validated operation")
    if category is ErrorCategory.RATE_LIMITED:
        headers: dict[str, str] | None = None
        if exc.failure.retry_at is not None:
            headers = {"Retry-After": format_datetime(exc.failure.retry_at, usegmt=True)}
        return _MappedFailure(429, "TIKTOK_RATE_LIMITED", "TikTok rate limited the operation", headers)
    if category is ErrorCategory.AMBIGUOUS_WRITE:
        return _MappedFailure(
            409,
            "TIKTOK_WRITE_AMBIGUOUS",
            "TikTok write result is ambiguous and requires manual reconciliation",
        )
    return _MappedFailure(502, "TIKTOK_UPSTREAM_UNAVAILABLE", "TikTok upstream is unavailable")


def _miaoshou_failure(exc: MiaoshouClientError) -> _MappedFailure:
    category = exc.failure.category
    if category is MiaoshouFailureCategory.AUTHORIZATION:
        return _MappedFailure(502, "MIAOSHOU_AUTHORIZATION_BLOCKED", "Miaoshou authorization was rejected")
    if category is MiaoshouFailureCategory.PERMISSION:
        return _MappedFailure(403, "MIAOSHOU_PERMISSION_BLOCKED", "Miaoshou endpoint permission is missing")
    if category is MiaoshouFailureCategory.RATE_LIMITED:
        return _MappedFailure(429, "MIAOSHOU_RATE_LIMITED", "Miaoshou rate limited the operation")
    if category is MiaoshouFailureCategory.VALIDATION:
        return _MappedFailure(422, "MIAOSHOU_REQUEST_REJECTED", "Miaoshou rejected the request")
    if category is MiaoshouFailureCategory.INVALID_RESPONSE:
        return _MappedFailure(502, "MIAOSHOU_RESPONSE_INVALID", "Miaoshou returned an invalid response")
    return _MappedFailure(502, "MIAOSHOU_UPSTREAM_UNAVAILABLE", "Miaoshou upstream is unavailable")


def _map_exception(exc: Exception) -> _MappedFailure:
    if isinstance(exc, ApiProblem):
        return _MappedFailure(exc.status_code, exc.code, exc.safe_message)
    if isinstance(exc, RequestValidationError):
        return _MappedFailure(422, "REQUEST_VALIDATION_FAILED", "request payload or parameters are invalid")
    if isinstance(exc, HTTPException):
        if exc.status_code == 404:
            return _MappedFailure(404, "RESOURCE_NOT_FOUND", "resource was not found")
        if exc.status_code == 405:
            return _MappedFailure(405, "METHOD_NOT_ALLOWED", "HTTP method is not allowed")
        return _MappedFailure(exc.status_code, "REQUEST_REJECTED", "request was rejected")
    if isinstance(exc, TikTokClientError):
        return _tiktok_failure(exc)
    if isinstance(exc, MiaoshouClientError):
        return _miaoshou_failure(exc)
    if isinstance(exc, DraftNotFound):
        return _MappedFailure(404, "DRAFT_NOT_FOUND", "product draft was not found")
    if isinstance(exc, (DraftConflict, IdempotencyConflict, ProductSubmissionInProgress, IntegrityError)):
        return _MappedFailure(409, "OPERATION_CONFLICT", "operation conflicts with persisted state")
    if isinstance(exc, ListingQuotaBlocked):
        return _MappedFailure(409, "LISTING_QUOTA_BLOCKED", "confirmed listing quota does not allow submission")
    if isinstance(exc, (CommerceAccessBlocked, ListingModeBlocked, ProductSubmissionBlocked)):
        return _MappedFailure(403, "COMMERCE_ACCESS_BLOCKED", "commerce preconditions are not satisfied")
    if isinstance(exc, (ProductGatewayError, OrderGatewayError, OrderPayloadError)):
        return _MappedFailure(502, "TIKTOK_RESPONSE_INVALID", "TikTok returned an invalid business response")
    if isinstance(exc, (AuthenticationError, SecurityConfigurationError)):
        return _MappedFailure(503, "BLOCKED_CONFIGURATION", "encrypted commerce credentials are unavailable")
    if isinstance(exc, (ProductPayloadError, ValueError)):
        return _MappedFailure(422, "BUSINESS_VALIDATION_FAILED", "business input is invalid")
    return _MappedFailure(500, "INTERNAL_ERROR", "an internal error occurred")


async def _exception_handler(request: Request, exc: Exception) -> JSONResponse:
    mapped = _map_exception(exc)
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=mapped.code,
            message=mapped.message,
            request_id=_request_id(request),
        )
    )
    return JSONResponse(
        status_code=mapped.status_code,
        content=envelope.model_dump(mode="json"),
        headers=mapped.headers,
    )


def install_api_errors(app: FastAPI) -> None:
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(ApiProblem, _exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, _exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _exception_handler)


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope},
    403: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope},
    422: {"model": ErrorEnvelope},
    429: {"model": ErrorEnvelope},
    500: {"model": ErrorEnvelope},
    502: {"model": ErrorEnvelope},
    503: {"model": ErrorEnvelope},
}