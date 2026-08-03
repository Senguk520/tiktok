"""TikTok OAuth URL and real token endpoint client."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from app.domain.scopes import ScopeSet


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    service_id: str
    app_key: str
    app_secret: str
    authorize_url: str = "https://services.tiktokshop.com/open/authorize"
    token_url: str = "https://auth.tiktok-shops.com/api/v2/token/get"
    timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OAuthConfig:
        values = os.environ if env is None else env
        required = {
            "service_id": values.get("TIKTOK_SERVICE_ID", "").strip(),
            "app_key": values.get("TIKTOK_APP_KEY", "").strip(),
            "app_secret": values.get("TIKTOK_APP_SECRET", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"missing TikTok OAuth settings: {', '.join(missing)}")
        return cls(**required)

    def authorization_url(self, state: str) -> str:
        if not state:
            raise ValueError("OAuth state is required")
        return f"{self.authorize_url}?{urlencode({'service_id': self.service_id, 'state': state})}"


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    refresh_token: str
    open_id: str
    user_type: int | str
    granted_scopes: ScopeSet
    access_expires_at: datetime
    refresh_expires_at: datetime

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> TokenSet:
        try:
            access_token = str(payload["access_token"])
            refresh_token = str(payload["refresh_token"])
            open_id = str(payload["open_id"])
            user_type = payload["user_type"]
            access_expiry = int(payload["access_token_expire_in"])
            refresh_expiry = int(payload["refresh_token_expire_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OAuthError("token response omitted required fields") from exc
        raw_scopes = payload.get("granted_scopes", [])
        if isinstance(raw_scopes, str):
            scope_values = [item for item in raw_scopes.replace(",", " ").split() if item]
        elif isinstance(raw_scopes, list):
            scope_values = [str(item) for item in raw_scopes]
        else:
            raise OAuthError("token response granted_scopes has invalid type")
        if not access_token or not refresh_token or not open_id:
            raise OAuthError("token response contains empty credentials")
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            open_id=open_id,
            user_type=user_type,
            granted_scopes=ScopeSet.parse(scope_values),
            access_expires_at=datetime.fromtimestamp(access_expiry, tz=UTC),
            refresh_expires_at=datetime.fromtimestamp(refresh_expiry, tz=UTC),
        )


class OAuthClient:
    def __init__(self, config: OAuthConfig) -> None:
        self._config = config

    def authorized_code_query(self, auth_code: str) -> Mapping[str, str]:
        if not auth_code:
            raise OAuthError("authorization code is required")
        return {
            "app_key": self._config.app_key,
            "app_secret": self._config.app_secret,
            "auth_code": auth_code,
            "grant_type": "authorized_code",
        }

    def refresh_query(self, refresh_token: str) -> Mapping[str, str]:
        if not refresh_token:
            raise OAuthError("refresh token is required")
        return {
            "app_key": self._config.app_key,
            "app_secret": self._config.app_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

    async def exchange_authorized_code(self, auth_code: str) -> TokenSet:
        return await self._token_request(self.authorized_code_query(auth_code))

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token_request(self.refresh_query(refresh_token))

    async def _token_request(self, query: Mapping[str, str]) -> TokenSet:
        async with httpx.AsyncClient(
            timeout=self._config.timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.get(self._config.token_url, params=query)
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise OAuthError(f"token endpoint returned HTTP {response.status_code}") from exc
        if not response.is_success or not isinstance(payload, dict):
            raise OAuthError(f"token endpoint returned HTTP {response.status_code}")
        code = payload.get("code")
        if code not in {None, 0, "0"}:
            raise OAuthError(f"token endpoint rejected request with code {code}")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise OAuthError("token endpoint response has no data object")
        return TokenSet.from_payload(data)