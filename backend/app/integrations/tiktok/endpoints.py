"""Single authoritative registry for independently versioned TikTok endpoints."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from app.domain.enums import Scope


class RetryPolicy(StrEnum):
    SAFE_READ = "SAFE_READ"
    IDEMPOTENT_WRITE = "IDEMPOTENT_WRITE"
    RECONCILE_THEN_RETRY = "RECONCILE_THEN_RETRY"
    NEVER = "NEVER"


class IdempotencyPolicy(StrEnum):
    NONE = "NONE"
    LOCAL_KEY = "LOCAL_KEY"
    SELLER_SKU_RECONCILIATION = "SELLER_SKU_RECONCILIATION"
    PLATFORM_CONFIRMED = "PLATFORM_CONFIRMED"


class ProductImageUseCase(StrEnum):
    MAIN_IMAGE = "MAIN_IMAGE"
    DESCRIPTION_IMAGE = "DESCRIPTION_IMAGE"
    CERTIFICATION_IMAGE = "CERTIFICATION_IMAGE"
    SIZE_CHART_IMAGE = "SIZE_CHART_IMAGE"


@dataclass(frozen=True, slots=True)
class Endpoint:
    key: str
    method: str
    path: str
    version: str
    scope: Scope
    write: bool
    retry: RetryPolicy
    idempotency: IdempotencyPolicy = IdempotencyPolicy.NONE
    enabled: bool = True
    official_anomaly: str | None = None

    def build_path(self, **path_parameters: str) -> str:
        try:
            rendered = self.path.format(**path_parameters)
        except KeyError as exc:
            raise ValueError(f"missing endpoint path parameter: {exc.args[0]}") from exc
        if "{" in rendered or "}" in rendered:
            raise ValueError("unresolved endpoint path parameter")
        return rendered

    @property
    def automatic_retry_allowed(self) -> bool:
        return self.enabled and self.retry in {
            RetryPolicy.SAFE_READ,
            RetryPolicy.IDEMPOTENT_WRITE,
            RetryPolicy.RECONCILE_THEN_RETRY,
        }


def _endpoint(
    key: str,
    method: str,
    path: str,
    scope: Scope,
    *,
    write: bool,
    retry: RetryPolicy,
    idempotency: IdempotencyPolicy = IdempotencyPolicy.NONE,
    enabled: bool = True,
    official_anomaly: str | None = None,
) -> Endpoint:
    segments = path.split("/")
    try:
        version = next(segment for segment in segments if segment.isdigit() and len(segment) == 6)
    except StopIteration as exc:
        raise ValueError(f"endpoint {key} does not include a six-digit version") from exc
    return Endpoint(
        key=key,
        method=method,
        path=path,
        version=version,
        scope=scope,
        write=write,
        retry=retry,
        idempotency=idempotency,
        enabled=enabled,
        official_anomaly=official_anomaly,
    )


_ITEMS = (
    _endpoint(
        "authorization.shops",
        "GET",
        "/authorization/202309/shops",
        Scope.AUTHORIZATION_INFO,
        write=False,
        retry=RetryPolicy.SAFE_READ,
    ),
    _endpoint(
        "logistics.warehouses",
        "GET",
        "/logistics/202309/warehouses",
        Scope.LOGISTICS,
        write=False,
        retry=RetryPolicy.SAFE_READ,
    ),
    _endpoint(
        "logistics.delivery_options",
        "GET",
        "/logistics/202309/warehouses/{warehouse_id}/delivery_options",
        Scope.LOGISTICS,
        write=False,
        retry=RetryPolicy.SAFE_READ,
    ),
    _endpoint(
        "product.image.upload",
        "POST",
        "/product/202309/images/upload",
        Scope.PRODUCT_WRITE,
        write=True,
        retry=RetryPolicy.NEVER,
    ),
    _endpoint(
        "local.create",
        "POST",
        "/product/202309/products",
        Scope.PRODUCT_WRITE,
        write=True,
        retry=RetryPolicy.RECONCILE_THEN_RETRY,
        idempotency=IdempotencyPolicy.SELLER_SKU_RECONCILIATION,
    ),
    _endpoint(
        "local.full_edit",
        "PUT",
        "/product/202509/products/{product_id}",
        Scope.PRODUCT_WRITE,
        write=True,
        retry=RetryPolicy.IDEMPOTENT_WRITE,
        idempotency=IdempotencyPolicy.LOCAL_KEY,
    ),
    _endpoint(
        "local.partial_edit",
        "POST",
        "/product/202509/products/{product_id}/partial_edit",
        Scope.PRODUCT_WRITE,
        write=True,
        retry=RetryPolicy.IDEMPOTENT_WRITE,
        idempotency=IdempotencyPolicy.LOCAL_KEY,
    ),
    _endpoint(
        "local.search",
        "POST",
        "/product/202502/products/search",
        Scope.PRODUCT_BASIC,
        write=False,
        retry=RetryPolicy.SAFE_READ,
    ),
    _endpoint(
        "local.get",
        "GET",
        "/product/202309/products/{product_id}",
        Scope.PRODUCT_BASIC,
        write=False,
        retry=RetryPolicy.SAFE_READ,
    ),
    _endpoint(
        "local.price",
        "POST",
        "/product/202309/products/{product_id}/prices/update",
        Scope.PRODUCT_WRITE,
        write=True,
        retry=RetryPolicy.IDEMPOTENT_WRITE,
        idempotency=IdempotencyPolicy.LOCAL_KEY,
    ),
    _endpoint(
        "local.inventory",
        "POST",
        "/product/202309/products/{product_id}/inventory/update",
        Scope.PRODUCT_WRITE,
        write=True,
        retry=RetryPolicy.IDEMPOTENT_WRITE,
        idempotency=IdempotencyPolicy.LOCAL_KEY,
    ),
    _endpoint(
        "local.activate",
        "POST",
        "/product/202309/products/activate",
        Scope.PRODUCT_BASIC,
        write=True,
        retry=RetryPolicy.IDEMPOTENT_WRITE,
        idempotency=IdempotencyPolicy.LOCAL_KEY,
    ),
    _endpoint(
        "local.deactivate",
        "POST",
        "/product/202309/products/deactivate",
        Scope.PRODUCT_BASIC,
        write=True,
        retry=RetryPolicy.IDEMPOTENT_WRITE,
        idempotency=IdempotencyPolicy.LOCAL_KEY,
    ),
    _endpoint(
        "local.delete",
        "DELETE",
        "/product/202309/products",
        Scope.PRODUCT_DELETE,
        write=True,
        retry=RetryPolicy.IDEMPOTENT_WRITE,
        idempotency=IdempotencyPolicy.LOCAL_KEY,
    ),
    _endpoint(
        "global.create",
        "POST",
        "/product/202309/global_products",
        Scope.GLOBAL_PRODUCT_WRITE,
        write=True,
        retry=RetryPolicy.RECONCILE_THEN_RETRY,
        idempotency=IdempotencyPolicy.SELLER_SKU_RECONCILIATION,
    ),
    _endpoint(
        "global.publish",
        "POST",
        "/product/202309/global_products/{global_product_id}/publish",
        Scope.GLOBAL_PRODUCT_WRITE,
        write=True,
        retry=RetryPolicy.IDEMPOTENT_WRITE,
        idempotency=IdempotencyPolicy.LOCAL_KEY,
    ),
    _endpoint(
        "global.full_edit",
        "PUT",
        "/product/202309/global_products/{global_product_id}",
        Scope.GLOBAL_PRODUCT_WRITE,
        write=True,
        retry=RetryPolicy.NEVER,
        idempotency=IdempotencyPolicy.LOCAL_KEY,
    ),
    _endpoint(
        "global.partial_edit_anomaly",
        "PUT",
        "/product/202509/global_products/{global_product_id}/partial_edit",
        Scope.PRODUCT_BASIC,
        write=True,
        retry=RetryPolicy.NEVER,
        enabled=False,
        official_anomaly="official method/description and scope metadata conflict",
    ),
    _endpoint(
        "global.search",
        "POST",
        "/product/202312/global_products/search",
        Scope.GLOBAL_PRODUCT_INFO,
        write=False,
        retry=RetryPolicy.SAFE_READ,
    ),
    _endpoint(
        "global.get",
        "GET",
        "/product/202309/global_products/{global_product_id}",
        Scope.GLOBAL_PRODUCT_INFO,
        write=False,
        retry=RetryPolicy.SAFE_READ,
    ),
    _endpoint(
        "global.delete",
        "DELETE",
        "/product/202309/global_products",
        Scope.GLOBAL_PRODUCT_DELETE,
        write=True,
        retry=RetryPolicy.IDEMPOTENT_WRITE,
        idempotency=IdempotencyPolicy.LOCAL_KEY,
    ),
    _endpoint(
        "global.inventory",
        "POST",
        "/product/202309/global_products/{global_product_id}/inventory/update",
        Scope.GLOBAL_PRODUCT_WRITE,
        write=True,
        retry=RetryPolicy.IDEMPOTENT_WRITE,
        idempotency=IdempotencyPolicy.LOCAL_KEY,
    ),
    _endpoint(
        "orders.search",
        "POST",
        "/order/202309/orders/search",
        Scope.ORDER_INFO,
        write=False,
        retry=RetryPolicy.SAFE_READ,
    ),
    _endpoint(
        "orders.detail",
        "GET",
        "/order/202507/orders",
        Scope.ORDER_INFO,
        write=False,
        retry=RetryPolicy.SAFE_READ,
    ),
)

if len({item.key for item in _ITEMS}) != len(_ITEMS):
    raise RuntimeError("TikTok endpoint keys must be unique")

ENDPOINTS: Mapping[str, Endpoint] = MappingProxyType({item.key: item for item in _ITEMS})


def endpoint(key: str, *, require_enabled: bool = True) -> Endpoint:
    try:
        selected = ENDPOINTS[key]
    except KeyError as exc:
        raise KeyError(f"unknown TikTok endpoint: {key}") from exc
    if require_enabled and not selected.enabled:
        raise PermissionError(f"TikTok endpoint is disabled: {key}")
    return selected