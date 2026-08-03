"""CJdropshipping V2 product-detail adapter using the documented official API."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import parse_qs, urlencode, urlsplit

from collector_app.outbound import (
    OutboundPolicy,
    OutboundRequestError,
    SafeHttpClient,
    SafeHttpResponse,
)
from collector_app.sources.contracts import (
    SourceAdapterError,
    SourceArtifact,
    SourceMode,
    SourceRequest,
)

CJ_SOURCE = "CJ"
CJ_PRODUCT_ENDPOINT = "https://developers.cjdropshipping.com/api2.0/v1/product/query"
_CJ_INPUT_HOSTS = frozenset(
    {
        "cjdropshipping.com",
        "www.cjdropshipping.com",
        "app.cjdropshipping.com",
        "developers.cjdropshipping.com",
    }
)
_PRODUCT_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class CjOfficialApiAdapter:
    """Read one CJ product through V2; credentials never enter job payloads."""

    source = CJ_SOURCE
    mode = SourceMode.OFFICIAL_API

    def __init__(
        self,
        *,
        access_token: str | None,
        http: SafeHttpClient | None = None,
    ) -> None:
        token = (access_token or "").strip()
        if "\r" in token or "\n" in token:
            raise ValueError("CJ access token is invalid")
        self._access_token = token
        self._http = http or SafeHttpClient(
            OutboundPolicy(allowed_hosts=frozenset({"developers.cjdropshipping.com"}))
        )

    async def collect(self, request: SourceRequest) -> SourceArtifact:
        _require_request(request, source=self.source, mode=self.mode)
        if not self._access_token:
            raise SourceAdapterError(
                "source_credentials_missing",
                "CJ official API credentials are not configured",
            )
        product_id = _extract_product_id(request)
        endpoint = f"{CJ_PRODUCT_ENDPOINT}?{urlencode({'pid': product_id})}"
        try:
            response = await self._http.get(
                endpoint,
                headers={
                    "Accept": "application/json",
                    "CJ-Access-Token": self._access_token,
                    "User-Agent": "single-shop-collector/0.1",
                },
            )
        except OutboundRequestError as exc:
            raise SourceAdapterError(exc.code, str(exc), retryable=exc.retryable) from exc
        _raise_for_http_status(response)
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise SourceAdapterError("invalid_source_response", "CJ response must be an object")
        code = payload.get("code")
        if code is not None and code not in (200, "200"):
            raise SourceAdapterError(
                "source_business_error",
                "CJ API rejected the product request",
                retryable=_retryable_business_code(code),
            )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise SourceAdapterError("invalid_source_response", "CJ response data must be an object")
        returned_id = data.get("pid")
        if returned_id is not None and str(returned_id) != product_id:
            raise SourceAdapterError(
                "source_identity_mismatch",
                "CJ response product identity does not match the request",
            )
        media_type = response.headers.get("content-type", "application/json").split(";", 1)[0].strip()
        return SourceArtifact(
            source=self.source,
            mode=self.mode,
            canonical_url=response.url,
            source_product_id=product_id,
            media_type=media_type or "application/json",
            body=response.content,
        )


def _require_request(request: SourceRequest, *, source: str, mode: SourceMode) -> None:
    if request.source != source or request.mode is not mode:
        raise SourceAdapterError("adapter_mismatch", "source request does not match adapter")


def _extract_product_id(request: SourceRequest) -> str:
    allowed_payload = {"product_id"}
    if set(request.payload) - allowed_payload:
        raise SourceAdapterError("invalid_source_request", "CJ request contains unsupported fields")
    explicit = request.payload.get("product_id")
    parsed = urlsplit(request.source_url)
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        raise SourceAdapterError("invalid_source_url", "CJ source URL must use HTTPS without credentials")
    host = (parsed.hostname or "").rstrip(".").lower()
    if host not in _CJ_INPUT_HOSTS:
        raise SourceAdapterError("invalid_source_url", "CJ source URL is not recognized")
    query = parse_qs(parsed.query, keep_blank_values=False)
    from_url = next(
        (
            values[0]
            for key in ("pid", "productId", "product_id")
            if (values := query.get(key))
        ),
        None,
    )
    product_id = str(explicit or from_url or "").strip()
    if not _PRODUCT_ID.fullmatch(product_id):
        raise SourceAdapterError("invalid_product_id", "CJ product ID is missing or invalid")
    if explicit is not None and from_url is not None and str(explicit).strip() != from_url:
        raise SourceAdapterError("source_identity_mismatch", "CJ URL and payload identify different products")
    return product_id


def _raise_for_http_status(response: SafeHttpResponse) -> None:
    status = response.status_code
    if status == 200:
        return
    if status in {401, 403}:
        raise SourceAdapterError("source_credentials_rejected", "CJ credentials were rejected")
    if status == 429:
        raise SourceAdapterError("source_rate_limited", "CJ rate limit was reached", retryable=True)
    if status >= 500:
        raise SourceAdapterError("source_unavailable", "CJ service is unavailable", retryable=True)
    raise SourceAdapterError("source_request_rejected", "CJ rejected the product request")


def _retryable_business_code(value: object) -> bool:
    rendered = str(value).strip()
    return rendered in {"429", "1600100", "1600101"}