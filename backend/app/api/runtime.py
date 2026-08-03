"""Process-local dependency graph; business facts remain in SQLite, not in caches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.tiktok.client import TikTokClient, TikTokConfig
from app.integrations.tiktok.orders import TikTokOrderGateway
from app.integrations.tiktok.products import TikTokProductGateway
from app.use_cases.commerce_context import CommerceAccessBlocked
from app.use_cases.orders import OrderService
from app.use_cases.products import ProductCapabilityEvidence, ProductService
from shared.security import KeyRing, SecurityConfigurationError, load_master_key_from_env


class _BlockedTikTokGateway:
    async def upload_image(self, *_args: Any, **_kwargs: Any) -> Any:
        raise CommerceAccessBlocked("TikTok platform credentials are not configured")

    async def create(self, *_args: Any, **_kwargs: Any) -> Any:
        raise CommerceAccessBlocked("TikTok platform credentials are not configured")

    async def search(self, *_args: Any, **_kwargs: Any) -> Any:
        raise CommerceAccessBlocked("TikTok platform credentials are not configured")

    async def details(self, *_args: Any, **_kwargs: Any) -> Any:
        raise CommerceAccessBlocked("TikTok platform credentials are not configured")


@dataclass(frozen=True, slots=True)
class CommerceRuntime:
    key_ring: KeyRing | None
    product_service: ProductService
    order_service: OrderService
    product_gateway: TikTokProductGateway | None
    platform_configured: bool
    master_key_configured: bool
    product_capabilities: ProductCapabilityEvidence


def build_commerce_runtime() -> CommerceRuntime:
    key_ring: KeyRing | None = None
    try:
        key_ring = KeyRing.from_current(load_master_key_from_env())
    except SecurityConfigurationError:
        pass

    client: TikTokClient | None = None
    try:
        client = TikTokClient(TikTokConfig.from_env())
    except ValueError:
        pass

    capabilities = ProductCapabilityEvidence()
    if client is None:
        blocked = _BlockedTikTokGateway()
        return CommerceRuntime(
            key_ring=key_ring,
            product_service=ProductService(blocked, capabilities=capabilities),
            order_service=OrderService(blocked),
            product_gateway=None,
            platform_configured=False,
            master_key_configured=key_ring is not None,
            product_capabilities=capabilities,
        )

    product_gateway = TikTokProductGateway(client)
    order_gateway = TikTokOrderGateway(client)
    return CommerceRuntime(
        key_ring=key_ring,
        product_service=ProductService(product_gateway, capabilities=capabilities),
        order_service=OrderService(order_gateway),
        product_gateway=product_gateway,
        platform_configured=True,
        master_key_configured=key_ring is not None,
        product_capabilities=capabilities,
    )