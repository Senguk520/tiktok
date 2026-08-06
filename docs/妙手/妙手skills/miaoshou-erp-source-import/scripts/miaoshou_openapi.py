#!/usr/bin/env python3
"""Shared Miaoshou ERP OpenAPI helpers for skill scripts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


DEFAULT_BASE_URL = "https://openapi-erp.91miaoshou.com"


class ConfigError(RuntimeError):
    """Raised when local OpenAPI configuration is missing or invalid."""


class ApiError(RuntimeError):
    """Raised when the HTTP request fails before a valid API payload is returned."""


@dataclass(frozen=True)
class MiaoshouConfig:
    app_key: str
    app_secret: str
    base_url: str = DEFAULT_BASE_URL
    timeout: int = 30
    account_id: str | None = None
    authorization: str | None = None
    cookie: str | None = None


def _skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_config_path() -> Path:
    return _skill_root() / "resources" / "config.json"


def load_config(config_path: str | os.PathLike[str] | None = None) -> MiaoshouConfig:
    """Load config from resources/config.json and environment overrides."""

    raw: dict[str, Any] = {}
    path = Path(config_path) if config_path else default_config_path()
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)

    app_key = os.getenv("MIAOSHOU_APP_KEY") or str(raw.get("app_key", "")).strip()
    app_secret = os.getenv("MIAOSHOU_APP_SECRET") or str(raw.get("app_secret", "")).strip()
    base_url = os.getenv("MIAOSHOU_BASE_URL") or str(raw.get("base_url", DEFAULT_BASE_URL)).strip()
    timeout_raw = os.getenv("MIAOSHOU_TIMEOUT") or raw.get("timeout", 30)
    account_id = os.getenv("MIAOSHOU_ACCOUNT_ID") or _optional_str(raw.get("account_id"))
    authorization = os.getenv("MIAOSHOU_AUTHORIZATION") or _optional_str(raw.get("authorization"))
    cookie = os.getenv("MIAOSHOU_COOKIE") or _optional_str(raw.get("cookie"))

    if not app_key or app_key == "your_app_key_here":
        raise ConfigError("Missing app_key. Set MIAOSHOU_APP_KEY or resources/config.json.")
    if not app_secret or app_secret == "your_app_secret_here":
        raise ConfigError("Missing app_secret. Set MIAOSHOU_APP_SECRET or resources/config.json.")

    try:
        timeout = int(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("timeout must be an integer number of seconds.") from exc

    return MiaoshouConfig(
        app_key=app_key,
        app_secret=app_secret,
        base_url=base_url.rstrip("/"),
        timeout=timeout,
        account_id=account_id,
        authorization=authorization,
        cookie=cookie,
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("optional_") or text.startswith("your_"):
        return None
    return text


def json_body(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def sign_headers(config: MiaoshouConfig, path: str, body_json: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    sign_content = (
        config.app_secret
        + path
        + timestamp
        + config.app_key
        + body_json
        + config.app_secret
    )
    signature = hmac.new(
        config.app_secret.encode("utf-8"),
        sign_content.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "x-app-key": config.app_key,
        "x-timestamp": timestamp,
        "x-sign": signature,
    }
    if config.account_id:
        headers["x-account-id"] = config.account_id
    if config.authorization:
        headers["authorization"] = config.authorization
    if config.cookie:
        headers["cookie"] = config.cookie
    return headers


def post_json(config: MiaoshouConfig, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    body = json_body(payload)
    headers = sign_headers(config, path, body)
    url = config.base_url + path
    request = urllib.request.Request(
        url=url,
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            response_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"HTTP {exc.code}: {error_text}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"Network error: {exc.reason}") from exc

    try:
        return json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ApiError("API returned non-JSON response.") from exc


def classify_api_code(code: str | None) -> str | None:
    hints = {
        "signMissing": "Missing signed headers.",
        "signExpired": "Local clock drift or non-seconds timestamp.",
        "signInvalid": "Signature mismatch. Check body JSON, path, app secret, and app key.",
        "appNotFound": "App key is wrong, disabled, or not approved.",
        "appNoPermission": "Open Platform app lacks endpoint permission.",
        "ipNotInWhitelist": "Caller IP is not in the account whitelist.",
    }
    return hints.get(code or "")
