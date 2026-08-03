"""Azure Translator v3.0 boundary with no response or translation cache."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.integrations.translation.contracts import (
    TranslationConfigurationBlocked,
    TranslationProvider,
    TranslationRequest,
    TranslationResult,
    TranslationUpstreamError,
)

_DEFAULT_ENDPOINT = "https://api.cognitive.microsofttranslator.com"
_ALLOWED_HOSTS = frozenset({"api.cognitive.microsofttranslator.com"})
_MAX_RESPONSE_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class AzureTranslatorConfig:
    subscription_key: str = field(repr=False)
    region: str
    endpoint: str = _DEFAULT_ENDPOINT
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        key = self.subscription_key.strip()
        region = self.region.strip()
        endpoint = self.endpoint.strip().rstrip("/")
        if not key or len(key) > 4096:
            raise TranslationConfigurationBlocked("Azure Translator subscription key is unavailable")
        if not region or len(region) > 128:
            raise TranslationConfigurationBlocked("Azure Translator region is unavailable")
        try:
            parsed = urlsplit(endpoint)
        except ValueError as exc:
            raise TranslationConfigurationBlocked("Azure Translator endpoint is invalid") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _ALLOWED_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise TranslationConfigurationBlocked("Azure Translator endpoint is not approved")
        if not 1 <= self.timeout_seconds <= 60:
            raise TranslationConfigurationBlocked("Azure Translator timeout is invalid")
        object.__setattr__(self, "subscription_key", key)
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "endpoint", endpoint)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AzureTranslatorConfig:
        values = os.environ if env is None else env
        key = values.get("AZURE_TRANSLATOR_KEY", "")
        region = values.get("AZURE_TRANSLATOR_REGION", "")
        endpoint = values.get("AZURE_TRANSLATOR_ENDPOINT", _DEFAULT_ENDPOINT)
        raw_timeout = values.get("AZURE_TRANSLATOR_TIMEOUT_SECONDS", "15")
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise TranslationConfigurationBlocked("Azure Translator timeout is invalid") from exc
        return cls(
            subscription_key=key,
            region=region,
            endpoint=endpoint,
            timeout_seconds=timeout,
        )


class AzureTranslator(TranslationProvider):
    def __init__(
        self,
        config: AzureTranslatorConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        headers = {
            "Ocp-Apim-Subscription-Key": self._config.subscription_key,
            "Ocp-Apim-Subscription-Region": self._config.region,
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
        }
        params = {
            "api-version": "3.0",
            "from": request.source_language,
            "to": request.target_language,
            "textType": "plain",
        }
        timeout = httpx.Timeout(self._config.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=self._config.endpoint,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    "/translate",
                    params=params,
                    headers=headers,
                    json=[{"Text": text} for text in request.texts],
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TranslationUpstreamError("azure_translator_unavailable", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise TranslationUpstreamError("azure_translator_transport_error") from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise TranslationUpstreamError("azure_translator_response_too_large")
        if response.status_code == 429:
            raise TranslationUpstreamError("azure_translator_rate_limited", retryable=True)
        if response.status_code in {401, 403}:
            raise TranslationUpstreamError("azure_translator_access_rejected")
        if response.status_code >= 500:
            raise TranslationUpstreamError("azure_translator_unavailable", retryable=True)
        if response.status_code != 200:
            raise TranslationUpstreamError("azure_translator_request_rejected")
        translated = _parse_translation_response(
            response,
            expected_count=len(request.texts),
            expected_language=request.target_language,
        )
        request_id = _bounded_request_id(response.headers)
        return TranslationResult(
            texts=translated,
            source_language=request.source_language,
            target_language=request.target_language,
            provider="AZURE_TRANSLATOR_V3",
            request_id=request_id,
        )


def _parse_translation_response(
    response: httpx.Response,
    *,
    expected_count: int,
    expected_language: str,
) -> tuple[str, ...]:
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise TranslationUpstreamError("azure_translator_response_invalid") from exc
    if not isinstance(payload, list) or len(payload) != expected_count:
        raise TranslationUpstreamError("azure_translator_response_invalid")
    results: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            raise TranslationUpstreamError("azure_translator_response_invalid")
        translations = item.get("translations")
        if not isinstance(translations, list) or len(translations) != 1:
            raise TranslationUpstreamError("azure_translator_response_invalid")
        translation = translations[0]
        if not isinstance(translation, dict) or translation.get("to") != expected_language:
            raise TranslationUpstreamError("azure_translator_response_invalid")
        text = translation.get("text")
        if not isinstance(text, str) or not text or len(text) > 20_000:
            raise TranslationUpstreamError("azure_translator_response_invalid")
        results.append(text)
    return tuple(results)


def _bounded_request_id(headers: httpx.Headers) -> str | None:
    value = headers.get("X-RequestId") or headers.get("X-MT-System")
    return value[:128] if value else None


__all__ = ["AzureTranslator", "AzureTranslatorConfig"]