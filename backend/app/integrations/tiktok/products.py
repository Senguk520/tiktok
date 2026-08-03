"""TikTok product gateway with one fail-closed Local/Global route per intent."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from app.domain.enums import ListingMode, Scope
from app.domain.product import NormalizedProduct
from app.integrations.tiktok.client import TikTokClient
from app.integrations.tiktok.endpoints import ProductImageUseCase
from app.use_cases.commerce_context import CommerceAccessBlocked, ShopAccessContext


class ProductGatewayError(RuntimeError):
    """Raised when a successful platform response lacks required product facts."""


@dataclass(frozen=True, slots=True)
class ProductSubmission:
    mode: ListingMode
    product_id: str
    request_id: str | None
    raw_status: str | None = None


@dataclass(frozen=True, slots=True)
class ProductPage:
    mode: ListingMode
    items: tuple[Mapping[str, Any], ...]
    next_page_token: str | None
    total_count: int | None
    request_id: str | None


@dataclass(frozen=True, slots=True)
class UploadedProductImage:
    image_id: str
    request_id: str | None


_CREATE_ENDPOINTS = {
    ListingMode.LOCAL_REPLICATION: ("local.create", Scope.PRODUCT_WRITE),
    ListingMode.GLOBAL_LEGACY: ("global.create", Scope.GLOBAL_PRODUCT_WRITE),
}
_SEARCH_ENDPOINTS = {
    ListingMode.LOCAL_REPLICATION: ("local.search", Scope.PRODUCT_BASIC),
    ListingMode.GLOBAL_LEGACY: ("global.search", Scope.GLOBAL_PRODUCT_INFO),
}
_GET_ENDPOINTS = {
    ListingMode.LOCAL_REPLICATION: ("local.get", "product_id", Scope.PRODUCT_BASIC),
    ListingMode.GLOBAL_LEGACY: (
        "global.get",
        "global_product_id",
        Scope.GLOBAL_PRODUCT_INFO,
    ),
}
_DELETE_ENDPOINTS = {
    ListingMode.LOCAL_REPLICATION: ("local.delete", "product_ids", Scope.PRODUCT_DELETE),
    ListingMode.GLOBAL_LEGACY: (
        "global.delete",
        "global_product_ids",
        Scope.GLOBAL_PRODUCT_DELETE,
    ),
}


_RouteValue = TypeVar("_RouteValue")


def _mode_route(
    routes: Mapping[ListingMode, _RouteValue], context: ShopAccessContext
) -> _RouteValue:
    mode = context.require_listing_write()
    try:
        return routes[mode]
    except KeyError as exc:
        raise CommerceAccessBlocked("listing mode has no registered product gateway") from exc


def _attributes(values: Mapping[str, str]) -> list[dict[str, Any]]:
    return [
        {"id": key, "values": [{"id": value}]}
        for key, value in sorted(values.items())
    ]


def product_submission_payload(product: NormalizedProduct) -> dict[str, Any]:
    """Translate the normalized intent once for either create endpoint."""

    if not product.ready_for_platform_submission:
        raise ValueError("product draft is not ready for platform submission")
    return {
        "title": product.title,
        "description": product.description,
        "category_id": product.category_id,
        "main_images": [{"uri": image.local_image_id} for image in product.images],
        "product_attributes": _attributes(product.attributes),
        "skus": [
            {
                "seller_sku": sku.seller_sku,
                "sales_attributes": _attributes(sku.attributes),
                "price": {
                    "amount": format(sku.price, "f"),
                    "currency": sku.currency.upper(),
                },
                "inventory": [
                    {"warehouse_id": warehouse_id, "quantity": quantity}
                    for warehouse_id, quantity in sorted(sku.inventory_by_warehouse.items())
                ],
            }
            for sku in product.skus
        ],
    }


def _response_mapping(data: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ProductGatewayError(f"TikTok {label} response is not an object")
    return data


def _identifier(data: Any, keys: Sequence[str], *, label: str) -> str:
    payload = _response_mapping(data, label=label)
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ProductGatewayError(f"TikTok {label} response lacks an identifier")


class TikTokProductGateway:
    def __init__(self, client: TikTokClient) -> None:
        self._client = client

    async def upload_image(
        self,
        context: ShopAccessContext,
        *,
        content: bytes,
        filename: str,
        content_type: str,
        use_case: ProductImageUseCase | str = ProductImageUseCase.MAIN_IMAGE,
    ) -> UploadedProductImage:
        context.require_scopes(Scope.PRODUCT_WRITE)
        result = await self._client.upload_product_image(
            access_token=context.access_token,
            shop_cipher=context.shop_cipher,
            image=content,
            filename=filename,
            content_type=content_type,
            use_case=use_case,
        )
        image_id = _identifier(result.data, ("uri", "image_id", "id"), label="image upload")
        return UploadedProductImage(image_id=image_id, request_id=result.request_id)

    async def create(
        self,
        context: ShopAccessContext,
        product: NormalizedProduct,
        *,
        reconcile: Any = None,
    ) -> ProductSubmission:
        endpoint_key, scope = _mode_route(_CREATE_ENDPOINTS, context)
        context.require_scopes(scope)
        payload = product_submission_payload(product)
        result = await self._client.request(
            endpoint_key,
            access_token=context.access_token,
            shop_cipher=context.shop_cipher,
            json_body=payload,
            idempotency_registered=True,
            reconcile=reconcile,
        )
        keys = (
            ("product_id", "id")
            if context.listing_mode is ListingMode.LOCAL_REPLICATION
            else ("global_product_id", "product_id", "id")
        )
        response = _response_mapping(result.data, label="create product")
        return ProductSubmission(
            mode=context.listing_mode,
            product_id=_identifier(response, keys, label="create product"),
            request_id=result.request_id,
            raw_status=str(response["status"]) if response.get("status") is not None else None,
        )

    async def search(
        self,
        context: ShopAccessContext,
        *,
        page_size: int = 20,
        page_token: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> ProductPage:
        if not 1 <= page_size <= 100:
            raise ValueError("product page_size must be between 1 and 100")
        endpoint_key, scope = _mode_route(_SEARCH_ENDPOINTS, context)
        context.require_scopes(scope)
        query = {"page_size": page_size}
        if page_token:
            query["page_token"] = page_token
        result = await self._client.request(
            endpoint_key,
            access_token=context.access_token,
            shop_cipher=context.shop_cipher,
            query=query,
            json_body=dict(filters or {}),
        )
        data = _response_mapping(result.data, label="product search")
        rows = data.get("products", data.get("global_products", []))
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise ProductGatewayError("TikTok product search response has an invalid item list")
        next_token = data.get("next_page_token")
        total = data.get("total_count")
        return ProductPage(
            mode=context.listing_mode,
            items=tuple(row for row in rows if isinstance(row, Mapping)),
            next_page_token=str(next_token) if next_token else None,
            total_count=int(total) if isinstance(total, int) else None,
            request_id=result.request_id,
        )

    async def get(self, context: ShopAccessContext, product_id: str) -> Mapping[str, Any]:
        if not product_id.strip():
            raise ValueError("product id is required")
        endpoint_key, parameter, scope = _mode_route(_GET_ENDPOINTS, context)
        context.require_scopes(scope)
        result = await self._client.request(
            endpoint_key,
            access_token=context.access_token,
            shop_cipher=context.shop_cipher,
            path_parameters={parameter: product_id},
        )
        return _response_mapping(result.data, label="product detail")

    async def delete(
        self,
        context: ShopAccessContext,
        product_ids: Sequence[str],
        *,
        idempotency_registered: bool,
    ) -> str | None:
        cleaned = tuple(value.strip() for value in product_ids if value.strip())
        if not cleaned or len(cleaned) > 20 or len(set(cleaned)) != len(cleaned):
            raise ValueError("product delete requires 1-20 unique ids")
        endpoint_key, body_key, scope = _mode_route(_DELETE_ENDPOINTS, context)
        context.require_scopes(scope)
        result = await self._client.request(
            endpoint_key,
            access_token=context.access_token,
            shop_cipher=context.shop_cipher,
            json_body={body_key: list(cleaned)},
            idempotency_registered=idempotency_registered,
        )
        return result.request_id