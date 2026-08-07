"""Miaoshou MY shop-list live check with a single read-only capability."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import datetime

from app.integrations.miaoshou.client import (
    MiaoshouClient,
    MiaoshouClientError,
    MiaoshouConfig,
    MiaoshouConfigurationError,
    MiaoshouFailure,
    MiaoshouFailureCategory,
    miaoshou_enabled_from_env,
)
from app.integrations.miaoshou.shops import MiaoshouShopAdapter
from app.use_cases.miaoshou_shops import MiaoshouShop, MiaoshouShopQueryService
from live_checks.report import SCHEMA_VERSION, LiveCheckReport, checked_at_utc, resource_fingerprint

_CAPABILITY = "SHOP_LIST_READ"
_PROVIDER = "MIAOSHOU"
_PLATFORM = "tiktok"
_SITE = "MY"
_PAGE_SIZE = 100
_MAX_PAGES = 100


def _report(
    *,
    status: str,
    now: datetime | None,
    shops: Sequence[MiaoshouShop] = (),
    error_category: str | None,
    notes: str,
) -> LiveCheckReport:
    fingerprints = tuple(
        sorted(
            {
                resource_fingerprint(_PROVIDER, _PLATFORM, _SITE, shop.shop_id)
                for shop in shops
            }
        )
    )
    return LiveCheckReport(
        schema_version=SCHEMA_VERSION,
        checked_at_utc=checked_at_utc(now),
        capability=_CAPABILITY,
        provider=_PROVIDER,
        status=status,
        site=_SITE,
        shop_count=len(fingerprints),
        resource_fingerprints=fingerprints,
        error_category=error_category,
        notes=notes,
    )


def blocked_report(error_category: str, *, now: datetime | None = None) -> LiveCheckReport:
    notes_by_category = {
        "MIAOSHOU_PROVIDER_DISABLED": "PROVIDER_DISABLED",
        "BLOCKED_LIVE_CREDENTIALS": "LIVE_CREDENTIALS_UNAVAILABLE",
        "BLOCKED_CONFIGURATION": "CONFIGURATION_BLOCKED",
    }
    try:
        notes = notes_by_category[error_category]
    except KeyError as exc:
        raise ValueError("unsupported Miaoshou live-check blocker") from exc
    return _report(
        status="BLOCKED",
        now=now,
        error_category=error_category,
        notes=notes,
    )


def success_report(
    shops: Sequence[MiaoshouShop],
    *,
    now: datetime | None = None,
) -> LiveCheckReport:
    return _report(
        status="PASSED",
        now=now,
        shops=shops,
        error_category=None,
        notes="READ_ONLY_CHECK_SUCCEEDED",
    )


def failure_report(
    error_category: str,
    *,
    now: datetime | None = None,
) -> LiveCheckReport:
    return _report(
        status="FAILED",
        now=now,
        error_category=error_category,
        notes="PROVIDER_REQUEST_FAILED",
    )


def internal_failure_report(*, now: datetime | None = None) -> LiveCheckReport:
    return _report(
        status="FAILED",
        now=now,
        error_category="INTERNAL_ERROR",
        notes="CHECK_EXECUTION_FAILED",
    )


def _invalid_response() -> MiaoshouClientError:
    return MiaoshouClientError(MiaoshouFailure(MiaoshouFailureCategory.INVALID_RESPONSE))


async def _read_all_my_shops(service: MiaoshouShopQueryService) -> tuple[MiaoshouShop, ...]:
    shops: list[MiaoshouShop] = []
    for page_no in range(1, _MAX_PAGES + 1):
        page = await service.query(
            platform=_PLATFORM,
            site=_SITE,
            page_no=page_no,
            page_size=_PAGE_SIZE,
        )
        if page.page_no != page_no or page.page_size != _PAGE_SIZE:
            raise _invalid_response()
        if any(shop.platform != _PLATFORM or shop.site != _SITE for shop in page.items):
            raise _invalid_response()
        shops.extend(page.items)
        if page.next_page_no is None:
            return tuple(shops)
        if page.next_page_no != page_no + 1:
            raise _invalid_response()
    raise _invalid_response()


async def run_miaoshou_shop_check(
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> LiveCheckReport:
    """Run only the TikTok/MY shop-list read when explicitly enabled and configured."""

    values = os.environ if env is None else env
    try:
        if not miaoshou_enabled_from_env(values):
            return blocked_report("MIAOSHOU_PROVIDER_DISABLED", now=now)
        config = MiaoshouConfig.from_env(values)
    except MiaoshouConfigurationError as exc:
        if exc.code in {"BLOCKED_LIVE_CREDENTIALS", "BLOCKED_CONFIGURATION"}:
            return blocked_report(exc.code, now=now)
        return internal_failure_report(now=now)

    service = MiaoshouShopQueryService(MiaoshouShopAdapter(MiaoshouClient(config)))
    try:
        shops = await _read_all_my_shops(service)
        return success_report(shops, now=now)
    except MiaoshouClientError as exc:
        return failure_report(exc.failure.category.value, now=now)
    except Exception:
        return internal_failure_report(now=now)