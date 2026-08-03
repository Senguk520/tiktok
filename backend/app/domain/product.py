"""Normalized product values independent of TikTok and source adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    source_url: str
    role: str = "MAIN"
    local_image_id: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedSku:
    seller_sku: str
    price: Decimal
    currency: str
    inventory_by_warehouse: Mapping[str, int]
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.seller_sku.strip():
            raise ValueError("seller SKU is required")
        if self.price < 0:
            raise ValueError("price cannot be negative")
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError("currency must be a three-letter code")
        if any(quantity < 0 for quantity in self.inventory_by_warehouse.values()):
            raise ValueError("inventory cannot be negative")


@dataclass(frozen=True, slots=True)
class NormalizedProduct:
    title: str
    description: str
    category_id: str | None
    skus: tuple[NormalizedSku, ...]
    images: tuple[NormalizedImage, ...]
    attributes: Mapping[str, str] = field(default_factory=dict)
    source_trace: Mapping[str, str] = field(default_factory=dict)
    unmapped_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("title is required")
        if not self.skus:
            raise ValueError("at least one SKU is required")
        seller_skus = [sku.seller_sku for sku in self.skus]
        if len(seller_skus) != len(set(seller_skus)):
            raise ValueError("seller SKUs must be unique")

    @property
    def ready_for_listing(self) -> bool:
        return bool(
            self.category_id
            and self.images
            and not self.unmapped_warnings
            and all(sku.inventory_by_warehouse for sku in self.skus)
        )