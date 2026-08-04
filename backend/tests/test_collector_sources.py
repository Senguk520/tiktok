from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import pytest

from collector_app.outbound import (
    OutboundPolicy,
    OutboundPolicyError,
    SafeHttpClient,
    validate_outbound_url,
)
from collector_app.sources import (
    SourceAdapterError,
    SourceMode,
    SourceRequest,
    build_source_registry,
    default_source_registry,
)
from collector_app.sources.alibaba_1688_open import (
    Alibaba1688OpenPlatformConfig,
    sign_open_platform_request,
)
from collector_app.sources.contracts import SourceArtifact


async def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    assert port == 443
    return ("93.184.216.34",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://api.example.test/product", "https_required"),
        ("https://user:secret@api.example.test/product", "url_credentials_forbidden"),
        ("https://api.example.test:8443/product", "port_not_allowed"),
        ("https://api.example.test.evil.invalid/product", "host_not_allowed"),
        ("https://other.example.test/product", "host_not_allowed"),
    ],
)
async def test_outbound_policy_rejects_untrusted_url_shapes(url: str, code: str) -> None:
    with pytest.raises(OutboundPolicyError) as captured:
        await validate_outbound_url(
            url,
            policy=OutboundPolicy(allowed_hosts=frozenset({"api.example.test"})),
            resolver=_public_resolver,
        )
    assert captured.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.10.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "100.64.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "2001:db8::1",
        "2002:0a00:0001::1",
    ],
)
async def test_outbound_policy_rejects_every_non_public_dns_answer(address: str) -> None:
    async def resolver(host: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34", address)

    with pytest.raises(OutboundPolicyError) as captured:
        await validate_outbound_url(
            "https://api.example.test/product",
            policy=OutboundPolicy(allowed_hosts=frozenset({"api.example.test"})),
            resolver=resolver,
        )
    assert captured.value.code == "non_public_address"


@pytest.mark.asyncio
async def test_safe_client_pins_dns_and_preserves_logical_host_for_tls() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, headers={"Content-Type": "application/json"}, json={"ok": True})

    client = SafeHttpClient(
        OutboundPolicy(allowed_hosts=frozenset({"api.example.test"})),
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    response = await client.get("https://api.example.test/product?q=1")

    assert response.url == "https://api.example.test/product?q=1"
    assert len(calls) == 1
    assert calls[0].url.host == "93.184.216.34"
    assert calls[0].headers["host"] == "api.example.test"
    assert calls[0].headers["connection"] == "close"
    assert calls[0].extensions["sni_hostname"] == "api.example.test"


@pytest.mark.asyncio
async def test_safe_client_revalidates_redirect_and_blocks_private_target() -> None:
    calls = 0

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34",) if host == "api.example.test" else ("127.0.0.1",)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "https://cdn.example.test/secret"})

    client = SafeHttpClient(
        OutboundPolicy(allowed_hosts=frozenset({"api.example.test", "cdn.example.test"})),
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OutboundPolicyError) as captured:
        await client.get("https://api.example.test/product")
    assert captured.value.code == "non_public_address"
    assert calls == 1


@pytest.mark.asyncio
async def test_safe_client_strips_credentials_on_allowed_cross_host_redirect() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": "https://cdn.example.test/file"})
        return httpx.Response(200, content=b"ok")

    client = SafeHttpClient(
        OutboundPolicy(allowed_hosts=frozenset({"api.example.test", "cdn.example.test"})),
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    response = await client.get(
        "https://api.example.test/product",
        headers={"Authorization": "Bearer backend-secret"},
    )

    assert response.url == "https://cdn.example.test/file"
    assert calls[0].headers["authorization"] == "Bearer backend-secret"
    assert "authorization" not in calls[1].headers
    assert calls[1].headers["host"] == "cdn.example.test"


@pytest.mark.asyncio
async def test_safe_client_rejects_dns_answer_change_before_connect() -> None:
    answers = iter((("93.184.216.34",), ("93.184.216.35",)))
    transport_called = False

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        return next(answers)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal transport_called
        transport_called = True
        return httpx.Response(200)

    client = SafeHttpClient(
        OutboundPolicy(allowed_hosts=frozenset({"api.example.test"})),
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OutboundPolicyError) as captured:
        await client.get("https://api.example.test/product")
    assert captured.value.code == "dns_answer_changed"
    assert not transport_called


@dataclass(slots=True)
class _StubAdapter:
    source: str
    mode: SourceMode

    async def collect(self, request: SourceRequest) -> SourceArtifact:
        return SourceArtifact(
            source=self.source,
            mode=self.mode,
            canonical_url=request.source_url,
            source_product_id=None,
            media_type="text/plain",
            body=b"ok",
        )


def test_registry_is_exact_and_rejects_ambiguous_source_modes() -> None:
    official = _StubAdapter("DEMO", SourceMode.OFFICIAL_API)
    public = _StubAdapter("DEMO", SourceMode.PUBLIC_PAGE)
    registry = build_source_registry((official, public))

    assert registry.resolve(SourceRequest("demo", SourceMode.OFFICIAL_API, "https://example.test")) is official
    assert registry.resolve(SourceRequest("demo", SourceMode.PUBLIC_PAGE, "https://example.test")) is public
    with pytest.raises(SourceAdapterError, match="not registered"):
        registry.resolve(SourceRequest("other", SourceMode.PUBLIC_PAGE, "https://example.test"))
    with pytest.raises(ValueError, match="duplicate"):
        build_source_registry((official, official))


def test_default_registry_exposes_only_evidence_backed_modes() -> None:
    registry = default_source_registry(cj_access_token=None)
    assert [(item.source, item.mode.value) for item in registry.available] == [
        ("1688", "OFFICIAL_API"),
        ("1688", "PUBLIC_PAGE"),
        ("CJ", "OFFICIAL_API"),
    ]


@pytest.mark.asyncio
async def test_1688_official_mode_fails_closed_without_own_grant() -> None:
    registry = default_source_registry(cj_access_token=None)
    request = SourceRequest(
        "1688",
        SourceMode.OFFICIAL_API,
        "https://detail.1688.com/offer/12345.html",
    )
    with pytest.raises(SourceAdapterError) as captured:
        await registry.resolve(request).collect(request)
    assert captured.value.code == "source_credentials_missing"


@pytest.mark.asyncio
async def test_cj_adapter_uses_fixed_api_target_and_backend_token() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"code": 200, "result": True, "data": {"pid": "CJ12345"}}
        return httpx.Response(200, headers={"Content-Type": "application/json"}, content=json.dumps(body))

    http = SafeHttpClient(
        OutboundPolicy(allowed_hosts=frozenset({"developers.cjdropshipping.com"})),
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    registry = default_source_registry(cj_access_token="backend-secret", cj_http=http)
    adapter = registry.resolve(
        SourceRequest(
            "CJ",
            SourceMode.OFFICIAL_API,
            "https://www.cjdropshipping.com/product/item.html?pid=CJ12345",
        )
    )
    artifact = await adapter.collect(
        SourceRequest(
            "CJ",
            SourceMode.OFFICIAL_API,
            "https://www.cjdropshipping.com/product/item.html?pid=CJ12345",
        )
    )

    assert artifact.source_product_id == "CJ12345"
    assert len(seen) == 1
    assert seen[0].url.path == "/api2.0/v1/product/query"
    assert seen[0].url.params["pid"] == "CJ12345"
    assert seen[0].headers["cj-access-token"] == "backend-secret"


@pytest.mark.asyncio
async def test_cj_adapter_fails_closed_without_credentials_or_matching_identity() -> None:
    registry = default_source_registry(cj_access_token=None)
    request = SourceRequest(
        "CJ",
        SourceMode.OFFICIAL_API,
        "https://www.cjdropshipping.com/product/item.html?pid=CJ12345",
    )
    with pytest.raises(SourceAdapterError) as missing:
        await registry.resolve(request).collect(request)
    assert missing.value.code == "source_credentials_missing"

    mismatched = SourceRequest(
        "CJ",
        SourceMode.OFFICIAL_API,
        "https://www.cjdropshipping.com/product/item.html?pid=CJ12345",
        {"product_id": "OTHER123"},
    )
    configured = default_source_registry(cj_access_token="secret")
    with pytest.raises(SourceAdapterError) as identity:
        await configured.resolve(mismatched).collect(mismatched)
    assert identity.value.code == "source_identity_mismatch"


def test_1688_open_platform_signature_vector() -> None:
    signature = sign_open_platform_request(
        api_path="param2/1/com.alibaba.product/alibaba.product.get/app-key",
        parameters={
            "access_token": "access-token",
            "productID": "123456789",
            "webSite": "1688",
        },
        app_secret="app-secret",
    )
    assert signature == "93A389629D6D0D08CC3A537195DFF8E36E1D8DFC"


@pytest.mark.parametrize("app_key", ["../escape", "key/segment", "key?query", " key with spaces "])
def test_1688_open_platform_config_rejects_untrusted_app_keys(app_key: str) -> None:
    with pytest.raises(ValueError, match="app_key"):
        Alibaba1688OpenPlatformConfig(
            app_key=app_key,
            app_secret="app-secret",
            access_token="access-token",
        )


@pytest.mark.asyncio
async def test_1688_open_platform_uses_signed_first_party_gateway() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"productInfo": {"productID": 123456789}},
        )

    http = SafeHttpClient(
        OutboundPolicy(allowed_hosts=frozenset({"gw.open.1688.com"})),
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    registry = default_source_registry(
        cj_access_token=None,
        alibaba_1688_config=Alibaba1688OpenPlatformConfig(
            app_key="app-key",
            app_secret="app-secret",
            access_token="access-token",
        ),
        alibaba_1688_open_http=http,
    )
    request = SourceRequest(
        "1688",
        SourceMode.OFFICIAL_API,
        "https://detail.1688.com/offer/123456789.html",
    )
    artifact = await registry.resolve(request).collect(request)

    assert artifact.source_product_id == "123456789"
    assert artifact.canonical_url == "https://detail.1688.com/offer/123456789.html"
    assert len(seen) == 1
    assert seen[0].url.path == "/openapi/param2/1/com.alibaba.product/alibaba.product.get/app-key"
    assert seen[0].url.params["access_token"] == "access-token"
    assert seen[0].url.params["_aop_signature"] == "93A389629D6D0D08CC3A537195DFF8E36E1D8DFC"
    assert "app-secret" not in str(seen[0].url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("document", "code"),
    [
        ({"productInfo": {"productID": 987654321}}, "source_identity_mismatch"),
        (
            {"error_code": "InvalidAccessToken", "error_message": "access-token"},
            "source_business_error",
        ),
    ],
)
async def test_1688_open_platform_fails_closed_without_leaking_credentials(
    document: dict[str, object],
    code: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "application/json"}, json=document)

    http = SafeHttpClient(
        OutboundPolicy(allowed_hosts=frozenset({"gw.open.1688.com"})),
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    registry = default_source_registry(
        cj_access_token=None,
        alibaba_1688_config=Alibaba1688OpenPlatformConfig(
            app_key="app-key",
            app_secret="app-secret",
            access_token="access-token",
        ),
        alibaba_1688_open_http=http,
    )
    request = SourceRequest(
        "1688",
        SourceMode.OFFICIAL_API,
        "https://detail.1688.com/offer/123456789.html",
    )
    with pytest.raises(SourceAdapterError) as captured:
        await registry.resolve(request).collect(request)
    assert captured.value.code == code
    assert "access-token" not in str(captured.value)
    assert "app-secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_1688_open_platform_rejects_non_offer_url_even_with_product_id() -> None:
    registry = default_source_registry(
        cj_access_token=None,
        alibaba_1688_config=Alibaba1688OpenPlatformConfig(
            app_key="app-key",
            app_secret="app-secret",
            access_token="access-token",
        ),
    )
    request = SourceRequest(
        "1688",
        SourceMode.OFFICIAL_API,
        "https://detail.1688.com/not-an-offer",
        {"product_id": "123456789"},
    )
    with pytest.raises(SourceAdapterError) as captured:
        await registry.resolve(request).collect(request)
    assert captured.value.code == "invalid_source_url"


@pytest.mark.asyncio
async def test_1688_adapter_canonicalizes_offer_and_never_uses_job_headers() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=b"<html><title>public offer</title></html>",
        )

    http = SafeHttpClient(
        OutboundPolicy(allowed_hosts=frozenset({"detail.1688.com", "m.1688.com"})),
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    registry = default_source_registry(cj_access_token=None, alibaba_1688_http=http)
    request = SourceRequest(
        "1688",
        SourceMode.PUBLIC_PAGE,
        "https://m.1688.com/offer/123456789.html?spm=tracking#ignored",
    )
    artifact = await registry.resolve(request).collect(request)

    assert artifact.source_product_id == "123456789"
    assert artifact.canonical_url == "https://detail.1688.com/offer/123456789.html"
    assert seen[0].url.path == "/offer/123456789.html"
    assert not seen[0].url.query
    assert "cookie" not in seen[0].headers


@pytest.mark.asyncio
async def test_1688_adapter_rejects_non_offer_urls_and_payloads() -> None:
    registry = default_source_registry(cj_access_token=None)
    invalid = SourceRequest(
        "1688",
        SourceMode.PUBLIC_PAGE,
        "https://login.1688.com/member/signin.htm",
    )
    with pytest.raises(SourceAdapterError) as bad_url:
        await registry.resolve(invalid).collect(invalid)
    assert bad_url.value.code == "invalid_source_url"

    payload = SourceRequest(
        "1688",
        SourceMode.PUBLIC_PAGE,
        "https://detail.1688.com/offer/123456789.html",
        {"cookie": "secret"},
    )
    with pytest.raises(SourceAdapterError) as bad_payload:
        await registry.resolve(payload).collect(payload)
    assert bad_payload.value.code == "invalid_source_request"