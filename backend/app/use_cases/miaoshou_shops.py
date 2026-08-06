"""Application port and normalized models for read-only Miaoshou shop discovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

MIAOSHOU_TIKTOK_SITES: dict[str, frozenset[str]] = {
    "tiktok": frozenset({"ID", "VN", "TH", "MY", "PH", "BR", "MX", "ES", "FR", "GB", "US", "DE", "IT", "JP"}),
    "tiktokGlobal": frozenset({"TIKTOKGLOBAL", "TIKTOKGLOBALUS", "TIKTOKGLOBALEU"}),
}
MIAOSHOU_TIKTOK_PLATFORMS = frozenset(MIAOSHOU_TIKTOK_SITES)


@dataclass(frozen=True, slots=True)
class MiaoshouShopQuery:
    platform: str
    site: str
    page_no: int = 1
    page_size: int = 100

    def __post_init__(self) -> None:
        if self.platform not in MIAOSHOU_TIKTOK_PLATFORMS:
            raise ValueError("unsupported Miaoshou TikTok platform")
        if not self.site or self.site != self.site.strip().upper() or not self.site.isascii():
            raise ValueError("Miaoshou site must be a concrete uppercase ASCII selector")
        if self.site not in MIAOSHOU_TIKTOK_SITES[self.platform]:
            raise ValueError("unsupported Miaoshou site for the selected TikTok platform")
        if self.page_no < 1:
            raise ValueError("Miaoshou page number must be at least one")
        if not 1 <= self.page_size <= 100:
            raise ValueError("Miaoshou page size must be between one and 100")


@dataclass(frozen=True, slots=True)
class MiaoshouShop:
    shop_id: str
    shop_name: str | None
    platform: str
    site: str
    site_name: str | None
    status: str | None
    authorization_expires_at: str | None
    last_authorized_at: str | None
    parent_shop_id: str | None
    is_cross_border: bool | None
    is_global: bool | None


@dataclass(frozen=True, slots=True)
class MiaoshouShopPage:
    items: tuple[MiaoshouShop, ...]
    page_no: int
    page_size: int
    next_page_no: int | None


class MiaoshouShopProvider(Protocol):
    async def query_shops(self, query: MiaoshouShopQuery) -> MiaoshouShopPage: ...


class MiaoshouShopQueryService:
    """Execute one explicit provider query without fallback or side effects."""

    def __init__(self, provider: MiaoshouShopProvider) -> None:
        self._provider = provider

    async def query(
        self,
        *,
        platform: str,
        site: str,
        page_no: int = 1,
        page_size: int = 100,
    ) -> MiaoshouShopPage:
        query = MiaoshouShopQuery(
            platform=platform,
            site=site,
            page_no=page_no,
            page_size=page_size,
        )
        return await self._provider.query_shops(query)