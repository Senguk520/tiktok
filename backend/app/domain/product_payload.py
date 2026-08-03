"""Pure serialization and validation for persisted normalized product drafts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.product import NormalizedImage, NormalizedProduct, NormalizedSku


class ProductPayloadError(ValueError):
    """Raised when a persisted or API product payload violates the normalized contract."""


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProductPayloadError(f"{field} must be an object")
    return value


def _sequence(value: object, *, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ProductPayloadError(f"{field} must be an array")
    return value


def _text(value: object, *, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProductPayloadError(f"{field} must be non-empty text")
    return value.strip()


def _string_map(value: object, *, field: str) -> dict[str, str]:
    raw = _mapping(value, field=field)
    result: dict[str, str] = {}
    for key, item in raw.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(item, str):
            raise ProductPayloadError(f"{field} must contain text keys and values")
        result[key.strip()] = item.strip()
    return result


def normalized_product_to_payload(product: NormalizedProduct) -> dict[str, Any]:
    """Return a stable JSON-compatible representation used for hashes and drafts."""

    return {
        "title": product.title,
        "description": product.description,
        "category_id": product.category_id,
        "skus": [
            {
                "seller_sku": sku.seller_sku,
                "price": format(sku.price, "f"),
                "currency": sku.currency.upper(),
                "inventory_by_warehouse": dict(sorted(sku.inventory_by_warehouse.items())),
                "attributes": dict(sorted(sku.attributes.items())),
            }
            for sku in product.skus
        ],
        "images": [
            {
                "source_url": image.source_url,
                "role": image.role,
                "local_image_id": image.local_image_id,
            }
            for image in product.images
        ],
        "attributes": dict(sorted(product.attributes.items())),
        "source_trace": dict(sorted(product.source_trace.items())),
        "unmapped_warnings": list(product.unmapped_warnings),
    }


def normalized_product_from_payload(payload: Mapping[str, Any]) -> NormalizedProduct:
    """Rebuild the immutable domain value and reject weakly typed persisted data."""

    try:
        raw_skus = _sequence(payload.get("skus"), field="skus")
        skus: list[NormalizedSku] = []
        for index, item in enumerate(raw_skus):
            raw = _mapping(item, field=f"skus[{index}]")
            try:
                price = Decimal(str(raw.get("price")))
            except (InvalidOperation, ValueError) as exc:
                raise ProductPayloadError(f"skus[{index}].price is invalid") from exc
            inventory_raw = _mapping(
                raw.get("inventory_by_warehouse"),
                field=f"skus[{index}].inventory_by_warehouse",
            )
            inventory: dict[str, int] = {}
            for warehouse_id, quantity in inventory_raw.items():
                if not isinstance(warehouse_id, str) or not warehouse_id.strip():
                    raise ProductPayloadError("warehouse id must be non-empty text")
                if not isinstance(quantity, int) or isinstance(quantity, bool):
                    raise ProductPayloadError("inventory quantity must be an integer")
                inventory[warehouse_id.strip()] = quantity
            skus.append(
                NormalizedSku(
                    seller_sku=_text(
                        raw.get("seller_sku"), field=f"skus[{index}].seller_sku"
                    )
                    or "",
                    price=price,
                    currency=_text(raw.get("currency"), field=f"skus[{index}].currency")
                    or "",
                    inventory_by_warehouse=inventory,
                    attributes=_string_map(
                        raw.get("attributes", {}), field=f"skus[{index}].attributes"
                    ),
                )
            )

        raw_images = _sequence(payload.get("images", []), field="images")
        images = tuple(
            NormalizedImage(
                source_url=_text(
                    _mapping(item, field=f"images[{index}]").get("source_url"),
                    field=f"images[{index}].source_url",
                )
                or "",
                role=_text(
                    _mapping(item, field=f"images[{index}]").get("role", "MAIN"),
                    field=f"images[{index}].role",
                )
                or "MAIN",
                local_image_id=_text(
                    _mapping(item, field=f"images[{index}]").get("local_image_id"),
                    field=f"images[{index}].local_image_id",
                    optional=True,
                ),
            )
            for index, item in enumerate(raw_images)
        )
        warnings_raw = _sequence(payload.get("unmapped_warnings", []), field="unmapped_warnings")
        if any(not isinstance(item, str) or not item.strip() for item in warnings_raw):
            raise ProductPayloadError("unmapped_warnings must contain non-empty text")
        return NormalizedProduct(
            title=_text(payload.get("title"), field="title") or "",
            description=str(payload.get("description") or ""),
            category_id=_text(payload.get("category_id"), field="category_id", optional=True),
            skus=tuple(skus),
            images=images,
            attributes=_string_map(payload.get("attributes", {}), field="attributes"),
            source_trace=_string_map(payload.get("source_trace", {}), field="source_trace"),
            unmapped_warnings=tuple(item.strip() for item in warnings_raw if isinstance(item, str)),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ProductPayloadError):
            raise
        raise ProductPayloadError(str(exc)) from exc


def bind_uploaded_image(
    product: NormalizedProduct,
    *,
    source_url: str,
    image_id: str,
) -> NormalizedProduct:
    """Return a copy with one exact source image bound to its TikTok image id."""

    if not image_id.strip():
        raise ProductPayloadError("image id is required")
    matched = False
    images: list[NormalizedImage] = []
    for image in product.images:
        if image.source_url == source_url:
            images.append(
                NormalizedImage(
                    source_url=image.source_url,
                    role=image.role,
                    local_image_id=image_id.strip(),
                )
            )
            matched = True
        else:
            images.append(image)
    if not matched:
        raise ProductPayloadError("source image is not present in the product draft")
    return NormalizedProduct(
        title=product.title,
        description=product.description,
        category_id=product.category_id,
        skus=product.skus,
        images=tuple(images),
        attributes=product.attributes,
        source_trace=product.source_trace,
        unmapped_warnings=product.unmapped_warnings,
    )