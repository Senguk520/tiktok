"""Canonical, allowlisted collection intents shared by APIs and source adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import SplitResult, parse_qs, quote, urlsplit

from collector_app.sources.contracts import SourceMode

_CJ_INPUT_HOSTS = frozenset(
    {
        "cjdropshipping.com",
        "www.cjdropshipping.com",
        "app.cjdropshipping.com",
        "developers.cjdropshipping.com",
    }
)
_1688_INPUT_HOSTS = frozenset({"detail.1688.com", "m.1688.com"})
_CJ_PRODUCT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_1688_OFFER_PATH = re.compile(r"^/offer/(?P<product_id>[1-9][0-9]{4,30})\.html$")


class SourceIntentError(ValueError):
    """Stable validation failure for a browser-originated collection intent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    source: str
    mode: SourceMode
    source_product_id: str
    canonical_url: str


def normalize_source_identity(
    *,
    source: str,
    mode: SourceMode | str,
    source_url: str,
) -> SourceIdentity:
    """Accept only supported source/mode pairs and canonical product URLs."""

    normalized_source = source.strip().upper()
    try:
        normalized_mode = SourceMode(mode)
    except ValueError as exc:
        raise SourceIntentError("unsupported_source_mode", "source mode is unsupported") from exc
    if (normalized_source, normalized_mode) == ("CJ", SourceMode.OFFICIAL_API):
        return _normalize_cj(source_url)
    if normalized_source == "1688" and normalized_mode in {
        SourceMode.OFFICIAL_API,
        SourceMode.PUBLIC_PAGE,
    }:
        return _normalize_1688(source_url, mode=normalized_mode)
    raise SourceIntentError("unsupported_source", "source and mode are unsupported")


def _parse_https_url(source_url: str) -> SplitResult:
    if not isinstance(source_url, str) or not source_url or len(source_url) > 2048:
        raise SourceIntentError("invalid_source_url", "source URL is invalid")
    if any(ord(character) < 32 for character in source_url):
        raise SourceIntentError("invalid_source_url", "source URL is invalid")
    try:
        parsed = urlsplit(source_url.strip())
        approved_port = parsed.port in (None, 443)
    except ValueError as exc:
        raise SourceIntentError("invalid_source_url", "source URL is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.username
        or parsed.password
        or not approved_port
    ):
        raise SourceIntentError(
            "invalid_source_url",
            "source URL must use approved HTTPS origin",
        )
    return parsed


def _normalize_cj(source_url: str) -> SourceIdentity:
    parsed = _parse_https_url(source_url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if host not in _CJ_INPUT_HOSTS:
        raise SourceIntentError("invalid_source_url", "CJ source URL is not recognized")
    query = parse_qs(parsed.query, keep_blank_values=False)
    product_id = next(
        (
            values[0]
            for key in ("pid", "productId", "product_id")
            if (values := query.get(key))
        ),
        "",
    ).strip()
    if not _CJ_PRODUCT_ID.fullmatch(product_id):
        raise SourceIntentError("invalid_product_id", "CJ product ID is missing or invalid")
    canonical = (
        "https://www.cjdropshipping.com/product/item.html?pid="
        f"{quote(product_id, safe='-_')}"
    )
    return SourceIdentity(
        source="CJ",
        mode=SourceMode.OFFICIAL_API,
        source_product_id=product_id,
        canonical_url=canonical,
    )


def _normalize_1688(source_url: str, *, mode: SourceMode) -> SourceIdentity:
    parsed = _parse_https_url(source_url)
    host = (parsed.hostname or "").rstrip(".").lower()
    match = _1688_OFFER_PATH.fullmatch(parsed.path)
    if host not in _1688_INPUT_HOSTS or match is None:
        raise SourceIntentError("invalid_source_url", "1688 source URL is not recognized")
    product_id = match.group("product_id")
    return SourceIdentity(
        source="1688",
        mode=mode,
        source_product_id=product_id,
        canonical_url=f"https://detail.1688.com/offer/{product_id}.html",
    )


__all__ = [
    "SourceIdentity",
    "SourceIntentError",
    "normalize_source_identity",
]