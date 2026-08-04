"""Strict normalization of untrusted source artifacts into Core domain values."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from collector_app.sources.contracts import SourceAdapterError, SourceArtifact, SourceMode

_MAX_TITLE = 255
_MAX_DESCRIPTION = 20_000
_MAX_VARIANTS = 100
_MAX_IMAGES = 12
_MAX_ARTIFACT_BYTES = 3 * 1024 * 1024
_TEXT_SPACE = re.compile(r"\s+")
_ALLOWED_IMAGE_SCHEMES = frozenset({"https"})


@dataclass(frozen=True, slots=True)
class SourceSku:
    seller_sku: str
    price: Decimal
    currency: str
    attributes: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True)
class SourceImage:
    source_url: str
    role: str


@dataclass(frozen=True, slots=True)
class SourceProduct:
    title: str
    description: str
    category_id: str | None
    skus: tuple[SourceSku, ...]
    images: tuple[SourceImage, ...]
    attributes: Mapping[str, str]
    source_trace: Mapping[str, str]
    unmapped_warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))
        object.__setattr__(self, "source_trace", MappingProxyType(dict(self.source_trace)))


@dataclass(frozen=True, slots=True)
class NormalizedCollection:
    product: SourceProduct
    source_product_id: str


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        if tag.lower() in {"script", "style", "noscript"}:
            self.ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


class _JsonLdExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.documents: list[str] = []
        self._capture = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {key.lower(): (value or "").lower() for key, value in attrs}
        if attributes.get("type") == "application/ld+json":
            self._capture = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capture:
            document = "".join(self._parts).strip()
            if document:
                self.documents.append(document)
            self._capture = False
            self._parts = []


def normalize_artifact(artifact: SourceArtifact) -> NormalizedCollection:
    if len(artifact.body) > _MAX_ARTIFACT_BYTES:
        raise SourceAdapterError("source_response_too_large", "source artifact exceeds its size limit")
    if artifact.source == "CJ" and artifact.mode is SourceMode.OFFICIAL_API:
        return _normalize_cj(artifact)
    if artifact.source == "1688" and artifact.mode is SourceMode.OFFICIAL_API:
        return _normalize_1688_open(artifact)
    if artifact.source == "1688" and artifact.mode is SourceMode.PUBLIC_PAGE:
        return _normalize_1688(artifact)
    raise SourceAdapterError("normalizer_missing", "source artifact has no registered normalizer")


def _normalize_cj(artifact: SourceArtifact) -> NormalizedCollection:
    if artifact.media_type != "application/json":
        raise SourceAdapterError("invalid_source_response", "CJ artifact must be JSON")
    try:
        document = json.loads(artifact.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceAdapterError("invalid_source_response", "CJ artifact contains invalid JSON") from exc
    root = _mapping(document, code="invalid_source_response", field="CJ response")
    data = _mapping(root.get("data"), code="invalid_source_response", field="CJ data")
    source_product_id = _bounded_identifier(
        data.get("pid") or artifact.source_product_id,
        field="CJ product ID",
    )
    if artifact.source_product_id and source_product_id != artifact.source_product_id:
        raise SourceAdapterError("source_identity_mismatch", "CJ artifact identity changed")
    title = _required_text(data.get("productNameEn"), field="CJ title", maximum=_MAX_TITLE)
    description = _plain_text(data.get("description", ""), maximum=_MAX_DESCRIPTION)
    variants = _sequence(data.get("variants"), field="CJ variants")
    if not variants or len(variants) > _MAX_VARIANTS:
        raise SourceAdapterError("invalid_source_product", "CJ variant count is invalid")
    skus: list[SourceSku] = []
    for index, raw_variant in enumerate(variants):
        variant = _mapping(raw_variant, code="invalid_source_product", field="CJ variant")
        seller_sku = _bounded_identifier(
            variant.get("variantSku") or variant.get("vid"),
            field=f"CJ variant {index + 1} SKU",
        )
        price = _price(variant.get("variantSellPrice"), field=f"CJ variant {index + 1} price")
        variant_key = _optional_text(variant.get("variantKey"), maximum=255)
        skus.append(
            SourceSku(
                seller_sku=seller_sku,
                price=price,
                currency="USD",
                attributes={"source_variant": variant_key} if variant_key else {},
            )
        )
    _require_unique_skus(skus)
    image_urls = _image_urls(
        [data.get("bigImage"), *_sequence_or_empty(data.get("productImageSet"))]
    )
    if not image_urls:
        raise SourceAdapterError("invalid_source_product", "CJ product has no safe image URL")
    product = SourceProduct(
        title=title,
        description=description,
        category_id=None,
        skus=tuple(skus),
        images=tuple(
            SourceImage(source_url=url, role="MAIN" if index == 0 else "DETAIL")
            for index, url in enumerate(image_urls)
        ),
        attributes={},
        source_trace={
            "title": "CJ.productNameEn",
            "description": "CJ.description",
            "skus": "CJ.variants",
            "images": "CJ.productImageSet",
        },
        unmapped_warnings=(
            "tiktok_category_requires_manual_mapping",
            "warehouse_inventory_requires_manual_mapping",
        ),
    )
    return NormalizedCollection(product=product, source_product_id=source_product_id)


def _normalize_1688_open(artifact: SourceArtifact) -> NormalizedCollection:
    if artifact.media_type != "application/json":
        raise SourceAdapterError("invalid_source_response", "1688 Open Platform artifact must be JSON")
    try:
        document = json.loads(artifact.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceAdapterError(
            "invalid_source_response",
            "1688 Open Platform artifact contains invalid JSON",
        ) from exc
    root = _mapping(document, code="invalid_source_response", field="1688 response")
    product_info = _mapping(
        root.get("productInfo"),
        code="invalid_source_response",
        field="1688 productInfo",
    )
    source_product_id = _bounded_identifier(
        product_info.get("productID") or product_info.get("productId") or artifact.source_product_id,
        field="1688 product ID",
    )
    if artifact.source_product_id and source_product_id != artifact.source_product_id:
        raise SourceAdapterError("source_identity_mismatch", "1688 artifact identity changed")
    title = _required_text(product_info.get("subject"), field="1688 title", maximum=_MAX_TITLE)
    description = _plain_text(product_info.get("description", ""), maximum=_MAX_DESCRIPTION)
    category_value = product_info.get("categoryID") or product_info.get("categoryId")
    category_id = (
        _bounded_identifier(category_value, field="1688 category ID")
        if category_value is not None
        else None
    )
    image_container = product_info.get("image")
    if isinstance(image_container, Mapping):
        raw_images = (
            image_container.get("images")
            or image_container.get("imageURLs")
            or image_container.get("mainImages")
        )
    else:
        raw_images = product_info.get("images") or product_info.get("imageURLs")
    image_urls = _1688_open_image_urls(_sequence_or_empty(raw_images))
    if not image_urls:
        raise SourceAdapterError("invalid_source_product", "1688 product has no safe image URL")

    base_price = _first_1688_price(product_info.get("saleInfo"))
    raw_skus = _sequence_or_empty(product_info.get("skuInfos"))
    if len(raw_skus) > _MAX_VARIANTS:
        raise SourceAdapterError("invalid_source_product", "1688 SKU count is invalid")
    skus: list[SourceSku] = []
    for index, raw_sku in enumerate(raw_skus):
        sku = _mapping(raw_sku, code="invalid_source_product", field="1688 SKU")
        seller_sku = _bounded_identifier(
            sku.get("skuId") or sku.get("specId") or sku.get("skuCode"),
            field=f"1688 SKU {index + 1} ID",
        )
        price_value = sku.get("price") or sku.get("consignPrice") or base_price
        skus.append(
            SourceSku(
                seller_sku=seller_sku,
                price=_price(price_value, field=f"1688 SKU {index + 1} price"),
                currency="CNY",
                attributes=_1688_sku_attributes(sku.get("attributes")),
            )
        )
    if not skus:
        skus.append(
            SourceSku(
                seller_sku=f"1688-{source_product_id}",
                price=_price(base_price, field="1688 product price"),
                currency="CNY",
                attributes={},
            )
        )
    _require_unique_skus(skus)
    product = SourceProduct(
        title=title,
        description=description,
        category_id=category_id,
        skus=tuple(skus),
        images=tuple(
            SourceImage(source_url=url, role="MAIN" if index == 0 else "DETAIL")
            for index, url in enumerate(image_urls)
        ),
        attributes={},
        source_trace={
            "title": "1688.productInfo.subject",
            "description": "1688.productInfo.description",
            "category_id": "1688.productInfo.categoryID",
            "skus": "1688.productInfo.skuInfos",
            "images": "1688.productInfo.image.images",
        },
        unmapped_warnings=(
            "open_platform_facts_require_human_confirmation",
            "tiktok_category_requires_manual_mapping",
            "warehouse_inventory_requires_manual_mapping",
        ),
    )
    return NormalizedCollection(product=product, source_product_id=source_product_id)


def _first_1688_price(value: object) -> object:
    if not isinstance(value, Mapping):
        return None
    direct = value.get("price") or value.get("consignPrice")
    if direct is not None:
        return direct
    ranges = _sequence_or_empty(value.get("priceRanges"))
    for item in ranges:
        if isinstance(item, Mapping) and (price := item.get("price")) is not None:
            return price
    return None


def _1688_sku_attributes(value: object) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in _sequence_or_empty(value)[:30]:
        if not isinstance(item, Mapping):
            raise SourceAdapterError("invalid_source_product", "1688 SKU attribute is invalid")
        key_value = item.get("attributeID") or item.get("attributeId") or item.get("attributeDisplayName")
        item_value = (
            item.get("attributeValueID")
            or item.get("attributeValueId")
            or item.get("attValueID")
            or item.get("attributeValue")
            or item.get("customValueName")
        )
        key = _bounded_identifier(key_value, field="1688 SKU attribute key")
        rendered = _bounded_identifier(item_value, field="1688 SKU attribute value")
        if key in result and result[key] != rendered:
            raise SourceAdapterError("invalid_source_product", "1688 SKU attributes are duplicated")
        result[key] = rendered
    return result


def _normalize_1688(artifact: SourceArtifact) -> NormalizedCollection:
    if artifact.media_type not in {"text/html", "application/xhtml+xml"}:
        raise SourceAdapterError("invalid_source_response", "1688 artifact must be HTML")
    try:
        markup = artifact.body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SourceAdapterError("invalid_source_response", "1688 page is not valid UTF-8") from exc
    parser = _JsonLdExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise SourceAdapterError("source_layout_unsupported", "1688 page structure is unsupported") from exc
    product_data = _find_product_json_ld(parser.documents)
    if product_data is None:
        raise SourceAdapterError(
            "source_layout_unsupported",
            "1688 public page exposes no supported Product JSON-LD",
        )
    source_product_id = _bounded_identifier(
        artifact.source_product_id,
        field="1688 product ID",
    )
    title = _required_text(product_data.get("name"), field="1688 title", maximum=_MAX_TITLE)
    description = _plain_text(product_data.get("description", ""), maximum=_MAX_DESCRIPTION)
    image_value = product_data.get("image")
    image_urls = _image_urls(
        [image_value] if isinstance(image_value, str) else _sequence_or_empty(image_value)
    )
    if not image_urls:
        raise SourceAdapterError("invalid_source_product", "1688 product has no safe image URL")
    offers = _offers(product_data.get("offers"))
    if not offers or len(offers) > _MAX_VARIANTS:
        raise SourceAdapterError("invalid_source_product", "1688 offer count is invalid")
    skus: list[SourceSku] = []
    for index, offer in enumerate(offers):
        currency = _required_text(
            offer.get("priceCurrency"),
            field=f"1688 offer {index + 1} currency",
            maximum=3,
        ).upper()
        if len(currency) != 3 or not currency.isalpha():
            raise SourceAdapterError("invalid_source_product", "1688 offer currency is invalid")
        raw_sku = offer.get("sku") or product_data.get("sku") or f"1688-{source_product_id}-{index + 1}"
        skus.append(
            SourceSku(
                seller_sku=_bounded_identifier(raw_sku, field=f"1688 offer {index + 1} SKU"),
                price=_price(offer.get("price"), field=f"1688 offer {index + 1} price"),
                currency=currency,
                attributes={},
            )
        )
    _require_unique_skus(skus)
    product = SourceProduct(
        title=title,
        description=description,
        category_id=None,
        skus=tuple(skus),
        images=tuple(
            SourceImage(source_url=url, role="MAIN" if index == 0 else "DETAIL")
            for index, url in enumerate(image_urls)
        ),
        attributes={},
        source_trace={
            "title": "1688.ProductJSONLD.name",
            "description": "1688.ProductJSONLD.description",
            "skus": "1688.ProductJSONLD.offers",
            "images": "1688.ProductJSONLD.image",
        },
        unmapped_warnings=(
            "public_page_facts_require_human_confirmation",
            "tiktok_category_requires_manual_mapping",
            "warehouse_inventory_requires_manual_mapping",
        ),
    )
    return NormalizedCollection(product=product, source_product_id=source_product_id)


def _find_product_json_ld(documents: Sequence[str]) -> Mapping[str, Any] | None:
    for raw in documents[:20]:
        if len(raw.encode("utf-8")) > 512 * 1024:
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for candidate in _json_ld_candidates(value):
            raw_type = candidate.get("@type")
            types = [raw_type] if isinstance(raw_type, str) else _sequence_or_empty(raw_type)
            if any(isinstance(item, str) and item.lower() == "product" for item in types):
                return candidate
    return None


def _json_ld_candidates(value: object) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, Mapping):
        candidates = [value]
        graph = value.get("@graph")
        if isinstance(graph, Sequence) and not isinstance(graph, (str, bytes, bytearray)):
            candidates.extend(item for item in graph if isinstance(item, Mapping))
        return tuple(candidates)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(item for item in value if isinstance(item, Mapping))
    return ()


def _offers(value: object) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, Mapping):
        low = value.get("lowPrice")
        price = value.get("price", low)
        return ({**value, "price": price},) if price is not None else ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(item for item in value if isinstance(item, Mapping))
    return ()


def _mapping(value: object, *, code: str, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SourceAdapterError(code, f"{field} must be an object")
    return value


def _sequence(value: object, *, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise SourceAdapterError("invalid_source_product", f"{field} must be an array")
    return value


def _sequence_or_empty(value: object) -> tuple[object, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()


def _required_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise SourceAdapterError("invalid_source_product", f"{field} must be text")
    normalized = _TEXT_SPACE.sub(" ", html.unescape(value)).strip()
    if not normalized or len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise SourceAdapterError("invalid_source_product", f"{field} length is invalid")
    return normalized


def _optional_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceAdapterError("invalid_source_product", "optional source text is invalid")
    normalized = _TEXT_SPACE.sub(" ", html.unescape(value)).strip()
    if len(normalized) > maximum:
        raise SourceAdapterError("invalid_source_product", "optional source text is too long")
    return normalized or None


def _plain_text(value: object, *, maximum: int) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > maximum * 8:
        raise SourceAdapterError("invalid_source_product", "source description is invalid")
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise SourceAdapterError("invalid_source_product", "source description is invalid") from exc
    normalized = _TEXT_SPACE.sub(" ", " ".join(parser.parts)).strip()
    if len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise SourceAdapterError("invalid_source_product", "source description is invalid")
    return normalized


def _bounded_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise SourceAdapterError("invalid_source_product", f"{field} is invalid")
    rendered = str(value).strip()
    if not rendered or len(rendered) > 128 or any(ord(character) < 33 for character in rendered):
        raise SourceAdapterError("invalid_source_product", f"{field} is invalid")
    return rendered


def _price(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise SourceAdapterError("invalid_source_product", f"{field} is invalid")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise SourceAdapterError("invalid_source_product", f"{field} is invalid") from exc
    if not amount.is_finite() or amount <= 0 or amount > Decimal("100000000"):
        raise SourceAdapterError("invalid_source_product", f"{field} is out of range")
    return amount


def _1688_open_image_urls(values: Sequence[object]) -> tuple[str, ...]:
    candidates: list[object] = []
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if candidate.startswith("img/") and len(candidate) <= 2000:
            candidate = f"https://cbu01.alicdn.com/{candidate}"
        candidates.append(candidate)
    return _image_urls(candidates)


def _image_urls(values: Sequence[object]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if (
            parsed.scheme.lower() not in _ALLOWED_IMAGE_SCHEMES
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or len(candidate) > 2048
        ):
            continue
        if candidate not in result:
            result.append(candidate)
        if len(result) == _MAX_IMAGES:
            break
    return tuple(result)


def _require_unique_skus(skus: Sequence[SourceSku]) -> None:
    identifiers = [item.seller_sku for item in skus]
    if len(identifiers) != len(set(identifiers)):
        raise SourceAdapterError("invalid_source_product", "source SKU identifiers are duplicated")