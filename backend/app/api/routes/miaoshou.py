"""Protected, read-only Core API routes for the optional Miaoshou provider."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from app.api.auth import AuthenticatedAdmin, require_admin_session
from app.api.dependencies import commerce_runtime
from app.api.errors import ERROR_RESPONSES, ApiProblem
from app.api.runtime import CommerceRuntime
from app.use_cases.miaoshou_shops import MiaoshouShopPage


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MiaoshouCapabilitiesResponse(_StrictModel):
    provider: Literal["miaoshou"]
    configured: bool
    shop_query_enabled: bool
    blockers: list[str]


class MiaoshouShopResponse(_StrictModel):
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


class MiaoshouShopPageResponse(_StrictModel):
    provider: Literal["miaoshou"]
    platform: str
    site: str
    page_no: int
    page_size: int
    next_page_no: int | None
    items: list[MiaoshouShopResponse]


router = APIRouter(prefix="/api/miaoshou", tags=["miaoshou"], responses=ERROR_RESPONSES)


_BLOCKER_MESSAGES = {
    "MIAOSHOU_PROVIDER_DISABLED": "Miaoshou provider is disabled",
    "BLOCKED_LIVE_CREDENTIALS": "Miaoshou live credentials are not configured",
    "BLOCKED_CONFIGURATION": "Miaoshou provider configuration is invalid",
}


def _require_shop_service(runtime: CommerceRuntime):
    if runtime.miaoshou_shop_service is None:
        blocker = runtime.miaoshou_blocker or "BLOCKED_CONFIGURATION"
        raise ApiProblem(503, blocker, _BLOCKER_MESSAGES.get(blocker, "Miaoshou provider is unavailable"))
    return runtime.miaoshou_shop_service


@router.get("/capabilities", response_model=MiaoshouCapabilitiesResponse)
async def miaoshou_capabilities(
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> MiaoshouCapabilitiesResponse:
    blocker = runtime.miaoshou_blocker if runtime.miaoshou_shop_service is None else None
    return MiaoshouCapabilitiesResponse(
        provider="miaoshou",
        configured=runtime.miaoshou_configured,
        shop_query_enabled=runtime.miaoshou_shop_service is not None,
        blockers=[blocker] if blocker else [],
    )


@router.get("/shops", response_model=MiaoshouShopPageResponse)
async def query_miaoshou_shops(
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
    platform: Annotated[Literal["tiktok", "tiktokGlobal"], Query()],
    site: Annotated[str, Query(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]+$")],
    page_no: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 100,
) -> MiaoshouShopPageResponse:
    service = _require_shop_service(runtime)
    page: MiaoshouShopPage = await service.query(
        platform=platform,
        site=site.strip().upper(),
        page_no=page_no,
        page_size=page_size,
    )
    return MiaoshouShopPageResponse(
        provider="miaoshou",
        platform=platform,
        site=site.strip().upper(),
        page_no=page.page_no,
        page_size=page.page_size,
        next_page_no=page.next_page_no,
        items=[
            MiaoshouShopResponse(
                shop_id=shop.shop_id,
                shop_name=shop.shop_name,
                platform=shop.platform,
                site=shop.site,
                site_name=shop.site_name,
                status=shop.status,
                authorization_expires_at=shop.authorization_expires_at,
                last_authorized_at=shop.last_authorized_at,
                parent_shop_id=shop.parent_shop_id,
                is_cross_border=shop.is_cross_border,
                is_global=shop.is_global,
            )
            for shop in page.items
        ],
    )