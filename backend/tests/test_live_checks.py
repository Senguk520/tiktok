from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.integrations.miaoshou.client import MiaoshouClient
from app.use_cases.miaoshou_shops import MiaoshouShop
from live_checks.miaoshou import blocked_report, run_miaoshou_shop_check, success_report
from live_checks.report import (
    LIVE_CHECK_FIELDS,
    LiveCheckFormatError,
    report_from_mapping,
    resource_fingerprint,
    serialize_report,
)
from live_checks.writer import atomic_write_report

_NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def test_live_check_serialization_uses_exact_allowlist() -> None:
    report = blocked_report("MIAOSHOU_PROVIDER_DISABLED", now=_NOW)

    document = json.loads(serialize_report(report))

    assert len(LIVE_CHECK_FIELDS) == 10
    assert tuple(document) == LIVE_CHECK_FIELDS
    assert set(document) == set(LIVE_CHECK_FIELDS)
    assert report_from_mapping(document) == report


def test_shop_identity_is_replaced_by_one_way_short_fingerprint() -> None:
    shop = MiaoshouShop(
        shop_id="sensitive-shop-id",
        shop_name="sensitive-shop-name",
        platform="tiktok",
        site="MY",
        site_name="Malaysia",
        status="ACTIVE",
        authorization_expires_at=None,
        last_authorized_at=None,
        parent_shop_id="sensitive-parent-id",
        is_cross_border=False,
        is_global=False,
    )

    body = serialize_report(success_report((shop,), now=_NOW)).decode("utf-8")
    fingerprint = resource_fingerprint("MIAOSHOU", "tiktok", "MY", shop.shop_id)

    assert fingerprint.startswith("sha256:")
    assert len(fingerprint) == len("sha256:") + 16
    assert fingerprint != resource_fingerprint("MIAOSHOU", "tiktok", "MY", "another-shop")
    assert fingerprint in body
    for sensitive_value in (shop.shop_id, shop.shop_name, shop.parent_shop_id, shop.site_name):
        assert sensitive_value not in body


@pytest.mark.parametrize(
    "field",
    ["app_secret", "access_token", "cookie", "request_signature", "shop_id", "shop_name", "raw_response"],
)
def test_sensitive_or_extra_fields_cannot_be_serialized(field: str) -> None:
    safe = blocked_report("MIAOSHOU_PROVIDER_DISABLED", now=_NOW).to_mapping()

    with pytest.raises(LiveCheckFormatError):
        report_from_mapping({**safe, field: "sensitive-value"})
    with pytest.raises(LiveCheckFormatError):
        serialize_report({**safe, field: "sensitive-value"})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_disabled_and_missing_credentials_reports_are_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected_post(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("disabled live check must not reach the provider")

    monkeypatch.setattr(MiaoshouClient, "post", unexpected_post)

    disabled = await run_miaoshou_shop_check(env={}, now=_NOW)
    missing_credentials = await run_miaoshou_shop_check(
        env={"MIAOSHOU_ENABLED": "true"},
        now=_NOW,
    )

    assert disabled == blocked_report("MIAOSHOU_PROVIDER_DISABLED", now=_NOW)
    assert missing_credentials == blocked_report("BLOCKED_LIVE_CREDENTIALS", now=_NOW)


@pytest.mark.asyncio
async def test_enabled_runner_reuses_only_tiktok_my_read_query(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[str, dict[str, object]]] = []

    async def controlled_post(
        _client: MiaoshouClient,
        path: str,
        payload: dict[str, object],
    ) -> object:
        observed.append((path, payload))
        return {
            "shopList": [
                {"shopId": "shop-one", "platform": "tiktok", "site": "MY"},
                {"shopId": "shop-two", "platform": "tiktok", "site": "MY"},
                {"shopId": "shop-one", "platform": "tiktok", "site": "MY"},
            ]
        }

    monkeypatch.setattr(MiaoshouClient, "post", controlled_post)

    report = await run_miaoshou_shop_check(
        env={
            "MIAOSHOU_ENABLED": "true",
            "MIAOSHOU_APP_KEY": "synthetic-app-key",
            "MIAOSHOU_APP_SECRET": "synthetic-app-secret",
        },
        now=_NOW,
    )

    assert observed == [
        (
            "/open/v1/product/shop/shop/get_shop_list",
            {"platform": "tiktok", "site": "MY", "pageNo": 1, "pageSize": 100},
        )
    ]
    assert report.status == "PASSED"
    assert report.shop_count == 2
    assert len(report.resource_fingerprints) == report.shop_count
    assert len(set(report.resource_fingerprints)) == report.shop_count


@pytest.mark.parametrize(
    ("platform", "site"),
    [("otherPlatform", "MY"), ("tiktok", "SG")],
)
@pytest.mark.asyncio
async def test_runner_rejects_out_of_scope_normalized_shop(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    site: str,
) -> None:
    async def controlled_post(
        _client: MiaoshouClient,
        _path: str,
        _payload: dict[str, object],
    ) -> object:
        return {
            "shopList": [
                {"shopId": "out-of-scope-shop", "platform": platform, "site": site}
            ]
        }

    monkeypatch.setattr(MiaoshouClient, "post", controlled_post)

    report = await run_miaoshou_shop_check(
        env={
            "MIAOSHOU_ENABLED": "true",
            "MIAOSHOU_APP_KEY": "synthetic-app-key",
            "MIAOSHOU_APP_SECRET": "synthetic-app-secret",
        },
        now=_NOW,
    )

    assert report.status == "FAILED"
    assert report.error_category == "INVALID_RESPONSE"
    assert report.shop_count == 0
    assert report.resource_fingerprints == ()


def test_atomic_writer_replaces_only_the_selected_tmp_path_file(tmp_path) -> None:
    target = tmp_path / "report.json"
    target.write_text("old", encoding="utf-8")
    report = blocked_report("MIAOSHOU_PROVIDER_DISABLED", now=_NOW)

    atomic_write_report(report, target)

    assert target.read_bytes() == serialize_report(report)
    assert tuple(path.name for path in tmp_path.iterdir()) == ("report.json",)