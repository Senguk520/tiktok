"""Versioned, strict Collector-to-Core product import contract.

This module contains values only. It has no database, HTTP, filesystem, or
service imports, so both processes can validate the same envelope without
sharing persistence ownership.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Final

CONTRACT_NAME: Final[str] = "collector.product-import"
CONTRACT_VERSION: Final[int] = 1
_ALLOWED_SOURCE_PAIRS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("CJ", "OFFICIAL_API"), ("1688", "OFFICIAL_API"), ("1688", "PUBLIC_PAGE")}
)
_ALLOWED_IMAGE_ROLES: Final[frozenset[str]] = frozenset({"MAIN", "DETAIL"})
_ALLOWED_IMAGE_TYPES: Final[frozenset[str]] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_MAX_IMAGE_BYTES: Final[int] = 5 * 1024 * 1024
_MAX_IMAGE_DIMENSION: Final[int] = 12_000
_MAX_IMAGE_PIXELS: Final[int] = 40_000_000


class CollectorContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CollectorSkuV1:
    seller_sku: str
    price: Decimal
    currency: str
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        seller_sku = _text(self.seller_sku, field="seller_sku", maximum=128)
        currency = _text(self.currency, field="currency", maximum=3).upper()
        if len(currency) != 3 or not currency.isalpha():
            raise CollectorContractError("currency must be a three-letter code")
        price = _decimal(self.price, field="price")
        if price <= 0 or price > Decimal("100000000"):
            raise CollectorContractError("price is out of range")
        attributes = _string_map(self.attributes, field="sku.attributes", maximum_items=30)
        object.__setattr__(self, "seller_sku", seller_sku)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "attributes", MappingProxyType(attributes))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "seller_sku": self.seller_sku,
            "price": format(self.price, "f"),
            "currency": self.currency,
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_mapping(cls, value: object) -> CollectorSkuV1:
        raw = _strict_mapping(
            value,
            field="sku",
            allowed={"seller_sku", "price", "currency", "attributes"},
            required={"seller_sku", "price", "currency", "attributes"},
        )
        return cls(
            seller_sku=raw["seller_sku"],
            price=_decimal(raw["price"], field="price"),
            currency=raw["currency"],
            attributes=_mapping(raw["attributes"], field="sku.attributes"),
        )


@dataclass(frozen=True, slots=True)
class CollectorImageV1:
    image_record_id: str
    role: str
    sha256: str
    content_type: str
    byte_size: int
    width: int
    height: int

    def __post_init__(self) -> None:
        image_record_id = _text(self.image_record_id, field="image_record_id", maximum=128)
        role = _text(self.role, field="image.role", maximum=16).upper()
        if role not in _ALLOWED_IMAGE_ROLES:
            raise CollectorContractError("image role is unsupported")
        digest = _sha256(self.sha256, field="image.sha256")
        content_type = _text(self.content_type, field="image.content_type", maximum=64).lower()
        if content_type not in _ALLOWED_IMAGE_TYPES:
            raise CollectorContractError("image content type is unsupported")
        if not isinstance(self.byte_size, int) or isinstance(self.byte_size, bool):
            raise CollectorContractError("image byte size must be an integer")
        if not 0 < self.byte_size <= _MAX_IMAGE_BYTES:
            raise CollectorContractError("image byte size is out of range")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in (self.width, self.height)):
            raise CollectorContractError("image dimensions must be integers")
        if (
            self.width <= 0
            or self.height <= 0
            or self.width > _MAX_IMAGE_DIMENSION
            or self.height > _MAX_IMAGE_DIMENSION
            or self.width * self.height > _MAX_IMAGE_PIXELS
        ):
            raise CollectorContractError("image dimensions are out of range")
        object.__setattr__(self, "image_record_id", image_record_id)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "content_type", content_type)

    def to_mapping(self) -> dict[str, str | int]:
        return {
            "image_record_id": self.image_record_id,
            "role": self.role,
            "sha256": self.sha256,
            "content_type": self.content_type,
            "byte_size": self.byte_size,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_mapping(cls, value: object) -> CollectorImageV1:
        fields = {
            "image_record_id",
            "role",
            "sha256",
            "content_type",
            "byte_size",
            "width",
            "height",
        }
        raw = _strict_mapping(value, field="image", allowed=fields, required=fields)
        return cls(
            image_record_id=raw["image_record_id"],
            role=raw["role"],
            sha256=raw["sha256"],
            content_type=raw["content_type"],
            byte_size=raw["byte_size"],
            width=raw["width"],
            height=raw["height"],
        )


@dataclass(frozen=True, slots=True)
class CollectorProductV1:
    title: str
    description: str
    category_id: str | None
    skus: tuple[CollectorSkuV1, ...]
    images: tuple[CollectorImageV1, ...]
    attributes: Mapping[str, str] = field(default_factory=dict)
    source_trace: Mapping[str, str] = field(default_factory=dict)
    unmapped_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        title = _text(self.title, field="title", maximum=255)
        description = _optional_text(self.description, field="description", maximum=20_000) or ""
        category_id = (
            None
            if self.category_id is None
            else _text(self.category_id, field="category_id", maximum=128)
        )
        skus = tuple(self.skus)
        images = tuple(self.images)
        if not 1 <= len(skus) <= 100 or any(not isinstance(item, CollectorSkuV1) for item in skus):
            raise CollectorContractError("SKUs are invalid")
        if not 1 <= len(images) <= 12 or any(
            not isinstance(item, CollectorImageV1) for item in images
        ):
            raise CollectorContractError("images are invalid")
        if len({item.seller_sku for item in skus}) != len(skus):
            raise CollectorContractError("seller SKUs must be unique")
        if len({item.image_record_id for item in images}) != len(images):
            raise CollectorContractError("image references must be unique")
        if sum(item.role == "MAIN" for item in images) != 1:
            raise CollectorContractError("exactly one MAIN image is required")
        attributes = _string_map(self.attributes, field="attributes", maximum_items=100)
        source_trace = _string_map(self.source_trace, field="source_trace", maximum_items=100)
        warnings = tuple(
            _text(item, field="unmapped_warning", maximum=255) for item in self.unmapped_warnings
        )
        if len(warnings) > 100 or len(set(warnings)) != len(warnings):
            raise CollectorContractError("unmapped warnings are invalid")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "category_id", category_id)
        object.__setattr__(self, "skus", skus)
        object.__setattr__(self, "images", images)
        object.__setattr__(self, "attributes", MappingProxyType(attributes))
        object.__setattr__(self, "source_trace", MappingProxyType(source_trace))
        object.__setattr__(self, "unmapped_warnings", warnings)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "title": self.title,
            "description": self.description,
            "category_id": self.category_id,
            "skus": [item.to_mapping() for item in self.skus],
            "images": [item.to_mapping() for item in self.images],
            "attributes": dict(self.attributes),
            "source_trace": dict(self.source_trace),
            "unmapped_warnings": list(self.unmapped_warnings),
        }

    @classmethod
    def from_mapping(cls, value: object) -> CollectorProductV1:
        fields = {
            "contract_version",
            "title",
            "description",
            "category_id",
            "skus",
            "images",
            "attributes",
            "source_trace",
            "unmapped_warnings",
        }
        raw = _strict_mapping(value, field="product", allowed=fields, required=fields)
        if raw["contract_version"] != CONTRACT_VERSION:
            raise CollectorContractError("unsupported product contract version")
        raw_skus = _bounded_sequence(raw["skus"], field="skus", minimum=1, maximum=100)
        raw_images = _bounded_sequence(raw["images"], field="images", minimum=1, maximum=12)
        raw_warnings = _bounded_sequence(
            raw["unmapped_warnings"], field="unmapped_warnings", minimum=0, maximum=100
        )
        return cls(
            title=raw["title"],
            description=raw["description"],
            category_id=raw["category_id"],
            skus=tuple(CollectorSkuV1.from_mapping(item) for item in raw_skus),
            images=tuple(CollectorImageV1.from_mapping(item) for item in raw_images),
            attributes=_mapping(raw["attributes"], field="attributes"),
            source_trace=_mapping(raw["source_trace"], field="source_trace"),
            unmapped_warnings=tuple(raw_warnings),
        )


@dataclass(frozen=True, slots=True)
class CollectorImportEnvelopeV1:
    result_id: str
    job_id: str
    source: str
    source_mode: str
    source_product_id: str
    product: CollectorProductV1
    digest: str | None = None

    def __post_init__(self) -> None:
        result_id = _text(self.result_id, field="result_id", maximum=128)
        job_id = _text(self.job_id, field="job_id", maximum=128)
        source = _text(self.source, field="source", maximum=32).upper()
        source_mode = _text(self.source_mode, field="source_mode", maximum=32).upper()
        source_product_id = _text(
            self.source_product_id,
            field="source_product_id",
            maximum=128,
        )
        if (source, source_mode) not in _ALLOWED_SOURCE_PAIRS:
            raise CollectorContractError("source identity is unsupported")
        if not isinstance(self.product, CollectorProductV1):
            raise CollectorContractError("product must use the current contract")
        expected_digest = _envelope_digest(
            result_id=result_id,
            job_id=job_id,
            source=source,
            source_mode=source_mode,
            source_product_id=source_product_id,
            product=self.product,
        )
        if self.digest is not None:
            supplied_digest = _sha256(self.digest, field="digest")
            if not hmac.compare_digest(supplied_digest, expected_digest):
                raise CollectorContractError("import envelope digest does not match its facts")
        object.__setattr__(self, "result_id", result_id)
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "source_mode", source_mode)
        object.__setattr__(self, "source_product_id", source_product_id)
        object.__setattr__(self, "digest", expected_digest)

    def to_mapping(self) -> dict[str, Any]:
        return {**self._unsigned_mapping(), "digest": self.digest}

    def _unsigned_mapping(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT_NAME,
            "version": CONTRACT_VERSION,
            "result_id": self.result_id,
            "job_id": self.job_id,
            "source": self.source,
            "source_mode": self.source_mode,
            "source_product_id": self.source_product_id,
            "product": self.product.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> CollectorImportEnvelopeV1:
        fields = {
            "contract",
            "version",
            "result_id",
            "job_id",
            "source",
            "source_mode",
            "source_product_id",
            "product",
            "digest",
        }
        raw = _strict_mapping(value, field="envelope", allowed=fields, required=fields)
        if raw["contract"] != CONTRACT_NAME or raw["version"] != CONTRACT_VERSION:
            raise CollectorContractError("unsupported import contract")
        return cls(
            result_id=raw["result_id"],
            job_id=raw["job_id"],
            source=raw["source"],
            source_mode=raw["source_mode"],
            source_product_id=raw["source_product_id"],
            product=CollectorProductV1.from_mapping(raw["product"]),
            digest=raw["digest"],
        )


@dataclass(frozen=True, slots=True)
class CollectorImportReceiptV1:
    result_id: str
    draft_id: str
    envelope_digest: str
    created: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _text(self.result_id, field="result_id", maximum=128))
        object.__setattr__(self, "draft_id", _text(self.draft_id, field="draft_id", maximum=128))
        object.__setattr__(
            self,
            "envelope_digest",
            _sha256(self.envelope_digest, field="envelope_digest"),
        )
        if not isinstance(self.created, bool):
            raise CollectorContractError("receipt creation marker must be boolean")


def _envelope_digest(
    *,
    result_id: str,
    job_id: str,
    source: str,
    source_mode: str,
    source_product_id: str,
    product: CollectorProductV1,
) -> str:
    payload = {
        "contract": CONTRACT_NAME,
        "version": CONTRACT_VERSION,
        "result_id": result_id,
        "job_id": job_id,
        "source": source,
        "source_mode": source_mode,
        "source_product_id": source_product_id,
        "product": product.to_mapping(),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise CollectorContractError(f"{field} must be an object")
    return value


def _strict_mapping(
    value: object,
    *,
    field: str,
    allowed: set[str],
    required: set[str],
) -> Mapping[str, Any]:
    raw = _mapping(value, field=field)
    keys = set(raw)
    if keys - allowed or required - keys:
        raise CollectorContractError(f"{field} fields are invalid")
    return raw


def _bounded_sequence(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CollectorContractError(f"{field} must be an array")
    if not minimum <= len(value) <= maximum:
        raise CollectorContractError(f"{field} count is out of range")
    return value


def _text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise CollectorContractError(f"{field} must be text")
    rendered = value.strip()
    if not rendered or len(rendered) > maximum or any(ord(character) < 32 for character in rendered):
        raise CollectorContractError(f"{field} is invalid")
    return rendered


def _optional_text(value: object, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise CollectorContractError(f"{field} is invalid")
    return value.strip() or None


def _decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise CollectorContractError(f"{field} must be numeric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise CollectorContractError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise CollectorContractError(f"{field} must be finite")
    return result


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise CollectorContractError(f"{field} must be text")
    digest = value.strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise CollectorContractError(f"{field} is invalid")
    return digest


def _string_map(value: object, *, field: str, maximum_items: int) -> dict[str, str]:
    raw = _mapping(value, field=field)
    if len(raw) > maximum_items:
        raise CollectorContractError(f"{field} has too many entries")
    result: dict[str, str] = {}
    for key, item in raw.items():
        clean_key = _text(key, field=f"{field}.key", maximum=128)
        clean_value = _text(item, field=f"{field}.value", maximum=512)
        result[clean_key] = clean_value
    return result