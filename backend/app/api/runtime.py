"""Process-local dependency graph; business facts remain in SQLite, not in caches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.miaoshou.client import (
    MiaoshouClient,
    MiaoshouConfig,
    MiaoshouConfigurationError,
    miaoshou_enabled_from_env,
)
from app.integrations.miaoshou.shops import MiaoshouShopAdapter
from app.integrations.tiktok.client import TikTokClient, TikTokConfig
from app.integrations.tiktok.orders import TikTokOrderGateway
from app.integrations.tiktok.products import TikTokProductGateway
from app.integrations.translation import (
    AzureTranslator,
    AzureTranslatorConfig,
    TranslationConfigurationBlocked,
    TranslationProvider,
)
from app.use_cases.commerce_context import CommerceAccessBlocked
from app.use_cases.miaoshou_shops import MiaoshouShopQueryService
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
    translation_provider: TranslationProvider | None
    translation_configured: bool
    miaoshou_shop_service: MiaoshouShopQueryService | None = None
    miaoshou_configured: bool = False
    miaoshou_blocker: str = "MIAOSHOU_PROVIDER_DISABLED"


def _build_translation_provider() -> TranslationProvider | None:
    try:
        return AzureTranslator(AzureTranslatorConfig.from_env())
    except TranslationConfigurationBlocked:
        return None


def _build_miaoshou_provider() -> tuple[MiaoshouShopQueryService | None, bool, str]:
    try:
        if not miaoshou_enabled_from_env():
            return None, False, "MIAOSHOU_PROVIDER_DISABLED"
        config = MiaoshouConfig.from_env()
    except MiaoshouConfigurationError as exc:
        return None, False, exc.code
    return MiaoshouShopQueryService(MiaoshouShopAdapter(MiaoshouClient(config))), True, ""


def build_commerce_runtime() -> CommerceRuntime:
    translation_provider = _build_translation_provider()
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

    miaoshou_service, miaoshou_configured, miaoshou_blocker = _build_miaoshou_provider()
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
            translation_provider=translation_provider,
            translation_configured=translation_provider is not None,
            miaoshou_shop_service=miaoshou_service,
            miaoshou_configured=miaoshou_configured,
            miaoshou_blocker=miaoshou_blocker,
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
        translation_provider=translation_provider,
        translation_configured=translation_provider is not None,
        miaoshou_shop_service=miaoshou_service,
        miaoshou_configured=miaoshou_configured,
        miaoshou_blocker=miaoshou_blocker,
    )
