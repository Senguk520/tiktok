"""1688 public product-page adapter.

No unverified private API, browser cookie, login automation, or third-party relay is
used here. The adapter only retrieves a bounded public offer page; parsing and
normalization remain a later worker responsibility and may fail when 1688 serves
an anti-bot or consent page.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

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

ALIBABA_1688_SOURCE = "1688"
_1688_HOSTS = frozenset({"detail.1688.com", "m.1688.com"})
_OFFER_PATH = re.compile(r"^/offer/(?P<product_id>[1-9][0-9]{4,30})\.html$")


class Alibaba1688PublicPageAdapter:
    """Retrieve an anonymous public offer page through the shared safe client."""

    source = ALIBABA_1688_SOURCE
    mode = SourceMode.PUBLIC_PAGE

    def __init__(self, *, http: SafeHttpClient | None = None) -> None:
        self._http = http or SafeHttpClient(
            OutboundPolicy(
                allowed_hosts=_1688_HOSTS,
                max_response_bytes=3 * 1024 * 1024,
            )
        )

    async def collect(self, request: SourceRequest) -> SourceArtifact:
        _require_request(request)
        product_id = _extract_product_id(request.source_url)
        if request.payload:
            raise SourceAdapterError(
                "invalid_source_request",
                "1688 public-page requests do not accept payload fields",
            )
        canonical_url = f"https://detail.1688.com/offer/{product_id}.html"
        try:
            response = await self._http.get(
                canonical_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "User-Agent": "single-shop-collector/0.1",
                },
            )
        except OutboundRequestError as exc:
            raise SourceAdapterError(exc.code, str(exc), retryable=exc.retryable) from exc
        _raise_for_http_status(response)
        if _extract_product_id(response.url) != product_id:
            raise SourceAdapterError(
                "source_identity_mismatch",
                "1688 redirected to a different product",
            )
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in {"text/html", "application/xhtml+xml"}:
            raise SourceAdapterError(
                "invalid_source_response",
                "1688 returned an unsupported document type",
            )
        return SourceArtifact(
            source=self.source,
            mode=self.mode,
            canonical_url=response.url,
            source_product_id=product_id,
            media_type=media_type,
            body=response.content,
        )


def _require_request(request: SourceRequest) -> None:
    if request.source != ALIBABA_1688_SOURCE or request.mode is not SourceMode.PUBLIC_PAGE:
        raise SourceAdapterError("adapter_mismatch", "source request does not match adapter")


def _extract_product_id(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        raise SourceAdapterError(
            "invalid_source_url",
            "1688 source URL must use HTTPS without credentials",
        )
    host = (parsed.hostname or "").rstrip(".").lower()
    if host not in _1688_HOSTS or parsed.port not in (None, 443):
        raise SourceAdapterError("invalid_source_url", "1688 source URL is not recognized")
    match = _OFFER_PATH.fullmatch(parsed.path)
    if match is None:
        raise SourceAdapterError("invalid_product_id", "1688 offer URL is missing a valid product ID")
    return match.group("product_id")


def _raise_for_http_status(response: SafeHttpResponse) -> None:
    status = response.status_code
    if status == 200:
        return
    if status == 404:
        raise SourceAdapterError("source_product_not_found", "1688 product was not found")
    if status in {401, 403}:
        raise SourceAdapterError(
            "source_access_blocked",
            "1688 did not expose the product as a public page",
        )
    if status == 429:
        raise SourceAdapterError("source_rate_limited", "1688 rate limit was reached", retryable=True)
    if status >= 500:
        raise SourceAdapterError("source_unavailable", "1688 service is unavailable", retryable=True)
    raise SourceAdapterError("source_request_rejected", "1688 rejected the product request")