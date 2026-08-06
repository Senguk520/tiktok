"""Miaoshou shop-list adapter; only normalized application models leave this module."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.integrations.miaoshou.client import (
    MiaoshouClient,
    MiaoshouClientError,
    MiaoshouFailure,
    MiaoshouFailureCategory,
)
from app.use_cases.miaoshou_shops import MiaoshouShop, MiaoshouShopPage, MiaoshouShopQuery

SHOP_LIST_PATH = "/open/v1/product/shop/shop/get_shop_list"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _flag(value: Any) -> bool | None:
    if value is None:
        return None
    if value in (True, 1, "1", "true", "TRUE", "Y", "y", "yes", "YES"):
        return True
    if value in (False, 0, "0", "false", "FALSE", "N", "n", "no", "NO"):
        return False
    return None


def _invalid_response() -> MiaoshouClientError:
    return MiaoshouClientError(
        MiaoshouFailure(MiaoshouFailureCategory.INVALID_RESPONSE)
    )


class MiaoshouShopAdapter:
    def __init__(self, client: MiaoshouClient) -> None:
        self._client = client

    async def query_shops(self, query: MiaoshouShopQuery) -> MiaoshouShopPage:
        payload = {
            "platform": query.platform,
            "site": query.site,
            "pageNo": query.page_no,
            "pageSize": query.page_size,
        }
        data = await self._client.post(SHOP_LIST_PATH, payload)
        if not isinstance(data, Mapping):
            raise _invalid_response()
        raw_items = data.get("shopList")
        if not isinstance(raw_items, list):
            raise _invalid_response()
        items: list[MiaoshouShop] = []
        try:
            for raw in raw_items:
                if not isinstance(raw, Mapping):
                    raise _invalid_response()
                shop_id = _text(raw.get("shopId"))
                if shop_id is None:
                    raise _invalid_response()
                items.append(
                    MiaoshouShop(
                        shop_id=shop_id,
                        shop_name=_text(raw.get("shopNick")),
                        platform=_text(raw.get("platform")) or query.platform,
                        site=_text(raw.get("site")) or query.site,
                        site_name=_text(raw.get("siteName")),
                        status=_text(raw.get("status")),
                        authorization_expires_at=_text(raw.get("gmtExpire")),
                        last_authorized_at=_text(raw.get("gmtLastAuth")),
                        parent_shop_id=_text(raw.get("parentShopId")),
                        is_cross_border=_flag(raw.get("isCb", raw.get("CB"))),
                        is_global=_flag(raw.get("isCnsc", raw.get("CNSC"))),
                    )
                )
        except (TypeError, ValueError) as exc:
            raise _invalid_response() from exc
        next_page = query.page_no + 1 if len(items) == query.page_size else None
        return MiaoshouShopPage(
            items=tuple(items),
            page_no=query.page_no,
            page_size=query.page_size,
            next_page_no=next_page,
        )