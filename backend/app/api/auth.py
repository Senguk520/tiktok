"""Local administrator session boundary with HttpOnly cookies and CSRF verification."""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import database_session, session_factory
from app.api.errors import ApiProblem
from app.db.models import AdminSession
from app.repositories.rate_limits import consume_rate_limit

SESSION_COOKIE_NAME = "tiktok_admin_session"


@dataclass(frozen=True, slots=True)
class AdminAuthSettings:
    bootstrap_secret: str | None = field(default=None, repr=False)
    session_ttl_seconds: int = 8 * 60 * 60
    secure_cookie: bool = False
    blocker: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AdminAuthSettings:
        values = os.environ if env is None else env
        secret = values.get("ADMIN_BOOTSTRAP_SECRET", "").strip()
        blocker: str | None = None
        if len(secret) < 32:
            secret = ""
            blocker = "ADMIN_BOOTSTRAP_SECRET must contain at least 32 characters"
        raw_ttl = values.get("ADMIN_SESSION_TTL_SECONDS", str(8 * 60 * 60)).strip()
        try:
            ttl = int(raw_ttl)
        except ValueError:
            ttl = 0
        if not 300 <= ttl <= 86_400:
            blocker = "ADMIN_SESSION_TTL_SECONDS must be between 300 and 86400"
        raw_secure = values.get("ADMIN_SESSION_COOKIE_SECURE", "false").strip().lower()
        if raw_secure not in {"true", "false"}:
            blocker = "ADMIN_SESSION_COOKIE_SECURE must be true or false"
        return cls(
            bootstrap_secret=secret or None,
            session_ttl_seconds=ttl if 300 <= ttl <= 86_400 else 8 * 60 * 60,
            secure_cookie=raw_secure == "true",
            blocker=blocker,
        )


@dataclass(frozen=True, slots=True)
class AuthenticatedAdmin:
    session_id: str
    csrf_digest: str = field(repr=False)
    expires_at: datetime


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap_secret: str = Field(min_length=1, max_length=4096, repr=False)


class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authenticated: bool
    csrf_token: str = Field(repr=False)
    expires_at: datetime


class SessionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authenticated: bool
    expires_at: datetime


class LogoutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authenticated: bool


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _auth_settings(request: Request) -> AdminAuthSettings:
    settings = getattr(request.app.state, "admin_auth_settings", None)
    if not isinstance(settings, AdminAuthSettings):
        raise ApiProblem(503, "BLOCKED_CONFIGURATION", "administrator authentication is unavailable")
    return settings


async def require_admin_session(
    request: Request,
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
) -> AuthenticatedAdmin:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not token:
        raise ApiProblem(401, "AUTHENTICATION_REQUIRED", "administrator session is required")
    async with factory.begin() as session:
        record = await session.scalar(
            select(AdminSession).where(AdminSession.session_digest == _digest(token))
        )
        now = datetime.now(UTC)
        if record is None or record.revoked_at is not None or _utc(record.expires_at) <= now:
            raise ApiProblem(401, "AUTHENTICATION_REQUIRED", "administrator session is invalid or expired")
        record.last_seen_at = now
        return AuthenticatedAdmin(
            session_id=record.id,
            csrf_digest=record.csrf_digest,
            expires_at=_utc(record.expires_at),
        )


async def require_csrf(
    request: Request,
    admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
) -> AuthenticatedAdmin:
    token = request.headers.get("X-CSRF-Token", "")
    supplied = _digest(token) if token else ""
    if not supplied or not secrets.compare_digest(supplied, admin.csrf_digest):
        raise ApiProblem(403, "CSRF_REJECTED", "CSRF verification failed")
    return admin


router = APIRouter(prefix="/api/session", tags=["session"])


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(database_session)],
) -> SessionResponse:
    settings = _auth_settings(request)
    if settings.blocker is not None or settings.bootstrap_secret is None:
        raise ApiProblem(503, "BLOCKED_CONFIGURATION", "administrator authentication is not configured")
    decision = await consume_rate_limit(
        session,
        app_key_hash=_digest("local-administrator-authentication"),
        shop_id="local-administrator",
        endpoint_key="admin.session.create",
        operation_type="AUTHENTICATE",
        limit_value=5,
        window_seconds=60,
    )
    if not decision.allowed:
        raise ApiProblem(429, "AUTHENTICATION_RATE_LIMITED", "administrator authentication is rate limited")
    if not secrets.compare_digest(payload.bootstrap_secret, settings.bootstrap_secret):
        raise ApiProblem(401, "AUTHENTICATION_FAILED", "administrator authentication failed")
    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)
    record = AdminSession(
        session_digest=_digest(session_token),
        csrf_digest=_digest(csrf_token),
        expires_at=expires_at,
    )
    session.add(record)
    await session.flush()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=settings.session_ttl_seconds,
        expires=expires_at,
        path="/api",
        secure=settings.secure_cookie,
        httponly=True,
        samesite="strict",
    )
    return SessionResponse(authenticated=True, csrf_token=csrf_token, expires_at=expires_at)


@router.get("", response_model=SessionStatusResponse)
async def session_status(
    admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
) -> SessionStatusResponse:
    return SessionStatusResponse(authenticated=True, expires_at=admin.expires_at)


@router.delete("", response_model=LogoutResponse)
async def revoke_session(
    request: Request,
    response: Response,
    admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(database_session)],
) -> LogoutResponse:
    record = await session.get(AdminSession, admin.session_id)
    if record is None:
        raise ApiProblem(401, "AUTHENTICATION_REQUIRED", "administrator session is invalid or expired")
    record.revoked_at = datetime.now(UTC)
    settings = _auth_settings(request)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/api",
        secure=settings.secure_cookie,
        httponly=True,
        samesite="strict",
    )
    return LogoutResponse(authenticated=False)