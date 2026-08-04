"""1688 Open Platform product adapter for the seller's own OAuth grant."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from collector_app.outbound import OutboundPolicy, OutboundRequestError, SafeHttpClient, SafeHttpResponse
from collector_app.sources.contracts import SourceAdapterError, SourceArtifact, SourceMode, SourceRequest
from collector_app.sources.intents import SourceIntentError, normalize_source_identity

ALIBABA_1688_SOURCE = "1688"
ALIBABA_1688_OPEN_HOST = "gw.open.1688.com"
ALIBABA_1688_API_NAMESPACE = "com.alibaba.product"
ALIBABA_1688_API_NAME = "alibaba.product.get"
_PRODUCT_ID = re.compile(r"^[1-9][0-9]{4,30}$")
_APP_KEY = re.compile(r"^[A-Za-z0-9_-]{3,128}$")


@dataclass(frozen=True, slots=True)
class Alibaba1688OpenPlatformConfig:
    app_key: str
    app_secret: str
    access_token: str

    def __post_init__(self) -> None:
        app_key = self.app_key.strip()
        if not _APP_KEY.fullmatch(app_key):
            raise ValueError("1688 app_key is invalid")
        object.__setattr__(self, "app_key", app_key)
        for field_name, value in (
            ("app_secret", self.app_secret),
            ("access_token", self.access_token),
        ):
            cleaned = value.strip()
            if not cleaned or len(cleaned) > 512 or "\r" in cleaned or "\n" in cleaned:
                raise ValueError(f"1688 {field_name} is invalid")
            object.__setattr__(self, field_name, cleaned)

    @property
    def api_path(self) -> str:
        return f"param2/1/{ALIBABA_1688_API_NAMESPACE}/{ALIBABA_1688_API_NAME}/{self.app_key}"

    @property
    def endpoint(self) -> str:
        return f"https://{ALIBABA_1688_OPEN_HOST}/openapi/{self.api_path}"


def sign_open_platform_request(
    *,
    api_path: str,
    parameters: Mapping[str, str],
    app_secret: str,
) -> str:
    """Sign the documented 1688 param2 path and sorted parameter pairs."""

    path = api_path.strip().lstrip("/")
    secret = app_secret.strip()
    if not path or not secret or any(not key or value is None for key, value in parameters.items()):
        raise ValueError("1688 signature inputs are invalid")
    signing_text = path + "".join(f"{key}{parameters[key]}" for key in sorted(parameters))
    return hmac.new(secret.encode("utf-8"), signing_text.encode("utf-8"), hashlib.sha1).hexdigest().upper()


class Alibaba1688OpenPlatformAdapter:
    """Read one product through an explicitly configured first-party Open Platform grant."""

    source = ALIBABA_1688_SOURCE
    mode = SourceMode.OFFICIAL_API

    def __init__(
        self,
        *,
        config: Alibaba1688OpenPlatformConfig | None,
        http: SafeHttpClient | None = None,
    ) -> None:
        self._config = config
        self._http = http or SafeHttpClient(
            OutboundPolicy(
                allowed_hosts=frozenset({ALIBABA_1688_OPEN_HOST}),
                max_response_bytes=3 * 1024 * 1024,
                max_redirects=0,
            )
        )

    async def collect(self, request: SourceRequest) -> SourceArtifact:
        if request.source != self.source or request.mode is not self.mode:
            raise SourceAdapterError("adapter_mismatch", "source request does not match adapter")
        if self._config is None:
            raise SourceAdapterError(
                "source_credentials_missing",
                "1688 Open Platform credentials are not configured",
            )
        product_id = _extract_product_id(request)
        parameters = {
            "access_token": self._config.access_token,
            "productID": product_id,
            "webSite": "1688",
        }
        signature = sign_open_platform_request(
            api_path=self._config.api_path,
            parameters=parameters,
            app_secret=self._config.app_secret,
        )
        endpoint = f"{self._config.endpoint}?{urlencode({**parameters, '_aop_signature': signature})}"
        try:
            response = await self._http.get(
                endpoint,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "single-shop-collector/0.1",
                },
            )
        except OutboundRequestError as exc:
            raise SourceAdapterError(exc.code, str(exc), retryable=exc.retryable) from exc
        _raise_for_http_status(response)
        document = _json_object(response)
        if _business_error(document):
            raise SourceAdapterError(
                "source_business_error",
                "1688 Open Platform rejected the product request",
                retryable=False,
            )
        returned_id = _response_product_id(document)
        if returned_id is None:
            raise SourceAdapterError(
                "invalid_source_response",
                "1688 Open Platform response contains no product identity",
            )
        if returned_id != product_id:
            raise SourceAdapterError(
                "source_identity_mismatch",
                "1688 response product identity does not match the request",
            )
        return SourceArtifact(
            source=self.source,
            mode=self.mode,
            canonical_url=f"https://detail.1688.com/offer/{product_id}.html",
            source_product_id=product_id,
            media_type="application/json",
            body=response.content,
        )


def _extract_product_id(request: SourceRequest) -> str:
    if set(request.payload) - {"product_id"}:
        raise SourceAdapterError("invalid_source_request", "1688 request contains unsupported fields")
    try:
        identity = normalize_source_identity(
            source=request.source,
            mode=request.mode,
            source_url=request.source_url,
        )
    except SourceIntentError as exc:
        raise SourceAdapterError(exc.code, str(exc)) from exc
    explicit = str(request.payload.get("product_id", "")).strip()
    if explicit and not _PRODUCT_ID.fullmatch(explicit):
        raise SourceAdapterError("invalid_product_id", "1688 offer URL is missing a valid product ID")
    if explicit and explicit != identity.source_product_id:
        raise SourceAdapterError("source_identity_mismatch", "1688 URL and payload identify different products")
    return identity.source_product_id


def _json_object(response: SafeHttpResponse) -> Mapping[str, object]:
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in {"application/json", "text/json"}:
        raise SourceAdapterError("invalid_source_response", "1688 Open Platform response must be JSON")
    try:
        value = json.loads(response.content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceAdapterError("invalid_source_response", "1688 Open Platform returned invalid JSON") from exc
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SourceAdapterError("invalid_source_response", "1688 Open Platform response must be an object")
    return value


def _business_error(document: Mapping[str, object]) -> bool:
    if document.get("success") is False:
        return True
    return any(
        document.get(field) not in (None, "", False, 0)
        for field in (
            "errorCode",
            "error_code",
            "errorMessage",
            "error_message",
            "exception",
            "errMsg",
        )
    )


def _response_product_id(document: Mapping[str, object]) -> str | None:
    product = document.get("productInfo")
    if not isinstance(product, Mapping):
        return None
    value = product.get("productID") or product.get("productId") or product.get("id")
    return str(value).strip() if value is not None and str(value).strip() else None


def _raise_for_http_status(response: SafeHttpResponse) -> None:
    if response.status_code == 200:
        return
    if response.status_code in {401, 403}:
        raise SourceAdapterError("source_credentials_rejected", "1688 credentials were rejected")
    if response.status_code == 429:
        raise SourceAdapterError("source_rate_limited", "1688 rate limit was reached", retryable=True)
    if response.status_code >= 500:
        raise SourceAdapterError("source_unavailable", "1688 service is unavailable", retryable=True)
    raise SourceAdapterError("source_request_rejected", "1688 rejected the product request")