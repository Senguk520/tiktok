from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from collector_app.db.base import CollectorDatabaseSettings
from collector_app.db.base import create_engine_and_session_factory as collector_factory
from collector_app.db.models import (
    CollectorAttempt,
    CollectorJob,
    CollectorResult,
    ImageRecord,
    SourceRateLimit,
)
from collector_app.db.repository import CollectorLeaseLost, claim_due_jobs, persist_failure, start_attempt
from collector_app.images import ImageDownloader, ImageTransformPolicy, StoredImage, inspect_image
from collector_app.normalizers import normalize_artifact
from collector_app.outbound import OutboundPolicy, SafeHttpClient
from collector_app.sources import (
    SourceAdapterError,
    SourceArtifact,
    SourceMode,
    SourceRequest,
    build_source_registry,
)
from collector_app.worker import CollectorWorker
from migrations.collector import migrate_engine as migrate_collector
from shared.collector_contract import CollectorProductV1
from shared.safe_paths import (
    COLLECTOR_IMAGE_DIR,
    PROJECT_ROOT,
    InvalidImageError,
    resolve_collector_image_path,
)

_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


async def _collector_database(
    tmp_path: Path,
) -> tuple[object, async_sessionmaker[AsyncSession]]:
    path = tmp_path / f"collector-{uuid4()}.sqlite3"
    settings = CollectorDatabaseSettings(url=f"sqlite+aiosqlite:///{path.as_posix()}", path=path)
    engine, factory = collector_factory(settings)
    await migrate_collector(engine)
    return engine, factory


def _cj_artifact(
    *,
    product_id: str = "CJ12345",
    image_url: str = "https://cf.cjdropshipping.com/p.png",
    detail_image_url: str | None = None,
) -> SourceArtifact:
    body = {
        "code": 200,
        "data": {
            "pid": product_id,
            "productNameEn": " Safe Product ",
            "description": "<p>Useful <script>ignore()</script>item</p>",
            "bigImage": image_url,
            "productImageSet": [detail_image_url or image_url],
            "variants": [
                {
                    "vid": "variant-1",
                    "variantSku": "CJ-SKU-1",
                    "variantSellPrice": "12.50",
                    "variantKey": "Black-M",
                }
            ],
        },
    }
    return SourceArtifact(
        source="CJ",
        mode=SourceMode.OFFICIAL_API,
        canonical_url=f"https://developers.cjdropshipping.com/api2.0/v1/product/query?pid={product_id}",
        source_product_id=product_id,
        media_type="application/json",
        body=json.dumps(body).encode(),
    )


@dataclass(slots=True)
class _FakeAdapter:
    callback: Any
    source: str = "CJ"
    mode: SourceMode = SourceMode.OFFICIAL_API

    async def collect(self, request: SourceRequest) -> SourceArtifact:
        return await self.callback(request)


@dataclass(slots=True)
class _FakeImages:
    downloaded: list[StoredImage] = field(default_factory=list)
    discarded: list[StoredImage] = field(default_factory=list)

    async def download(self, *, source: str, url: str) -> StoredImage:
        digest = f"{len(self.downloaded) + 1:064x}"
        image = StoredImage(
            relative_path=f"temp/images/collector/{uuid4()}.png",
            sha256=digest,
            content_type="image/png",
            byte_size=67,
            width=1,
            height=1,
        )
        self.downloaded.append(image)
        return image

    async def discard(self, image: StoredImage) -> bool:
        self.discarded.append(image)
        return True


@dataclass(slots=True)
class _SameContentImages(_FakeImages):
    async def download(self, *, source: str, url: str) -> StoredImage:
        image = StoredImage(
            relative_path=f"temp/images/collector/{uuid4()}.png",
            sha256="f" * 64,
            content_type="image/png",
            byte_size=67,
            width=1,
            height=1,
        )
        self.downloaded.append(image)
        return image


async def _add_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    source: str = "CJ",
    mode: str = "OFFICIAL_API",
    max_attempts: int = 3,
    request_hash: str | None = None,
) -> str:
    async with factory() as session:
        job = CollectorJob(
            source=source,
            source_mode=mode,
            source_url="https://www.cjdropshipping.com/product/item.html?pid=CJ12345",
            request_payload={},
            request_hash=request_hash or uuid4().hex.ljust(64, "0")[:64],
            max_attempts=max_attempts,
            next_attempt_at=_NOW - timedelta(seconds=1),
        )
        session.add(job)
        await session.commit()
        return job.id


@pytest.mark.asyncio
async def test_worker_commits_claim_before_network_and_persists_only_normalized_facts(tmp_path: Path) -> None:
    engine, factory = await _collector_database(tmp_path)
    job_id = await _add_job(factory)
    network_observed_committed_claim = False

    async def collect(request: SourceRequest) -> SourceArtifact:
        nonlocal network_observed_committed_claim
        async with factory() as independent:
            job = await independent.get(CollectorJob, job_id)
            attempt = await independent.scalar(
                select(CollectorAttempt).where(CollectorAttempt.collector_job_id == job_id)
            )
            assert job is not None and job.status == "RUNNING" and job.lease_owner == "worker-1"
            assert attempt is not None and attempt.attempt_number == 1
            independent.add(
                SourceRateLimit(
                    source="CJ",
                    mode="OFFICIAL_API",
                    window_started_at=_NOW,
                    window_seconds=60,
                    limit_value=10,
                )
            )
            await independent.commit()
            network_observed_committed_claim = True
        return _cj_artifact()

    images = _FakeImages()
    worker = CollectorWorker(
        session_factory=factory,
        registry=build_source_registry((_FakeAdapter(collect),)),
        images=images,  # type: ignore[arg-type]
        worker_id="worker-1",
        clock=lambda: _NOW,
    )
    try:
        outcomes = await worker.run_once(limit=1)
        assert outcomes == (outcomes[0],)
        assert outcomes[0].status == "SUCCEEDED"
        assert network_observed_committed_claim
        async with factory() as session:
            job = await session.get(CollectorJob, job_id)
            result = await session.scalar(
                select(CollectorResult).where(CollectorResult.collector_job_id == job_id)
            )
            attempt = await session.scalar(
                select(CollectorAttempt).where(CollectorAttempt.collector_job_id == job_id)
            )
            image = await session.scalar(select(ImageRecord))
        assert job is not None and job.status == "SUCCEEDED" and job.lease_owner is None
        assert attempt is not None and attempt.outcome == "SUCCEEDED" and attempt.error_redacted is None
        assert result is not None and result.source_product_id == "CJ12345"
        contract = CollectorProductV1.from_mapping(result.normalized_product)
        assert contract.title == "Safe Product"
        assert contract.description == "Useful item"
        assert contract.skus[0].seller_sku == "CJ-SKU-1"
        assert "code" not in result.normalized_product
        assert "developers.cjdropshipping.com" not in json.dumps(result.normalized_product)
        assert image is not None and image.ready and not Path(image.relative_path).is_absolute()
        assert image.source_url_redacted == "https://cf.cjdropshipping.com/p.png"
        assert not images.discarded
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_worker_deduplicates_equal_image_content_without_breaking_contract(tmp_path: Path) -> None:
    engine, factory = await _collector_database(tmp_path)
    job_id = await _add_job(factory)

    async def collect(request: SourceRequest) -> SourceArtifact:
        return _cj_artifact(
            image_url="https://cf.cjdropshipping.com/main.png",
            detail_image_url="https://cf.cjdropshipping.com/detail.png",
        )

    images = _SameContentImages()
    worker = CollectorWorker(
        session_factory=factory,
        registry=build_source_registry((_FakeAdapter(collect),)),
        images=images,  # type: ignore[arg-type]
        worker_id="worker-1",
        clock=lambda: _NOW,
    )
    try:
        outcome = (await worker.run_once(limit=1))[0]
        assert outcome.status == "SUCCEEDED"
        async with factory() as session:
            result = await session.scalar(
                select(CollectorResult).where(CollectorResult.collector_job_id == job_id)
            )
            image_records = tuple(await session.scalars(select(ImageRecord)))
        assert result is not None
        contract = CollectorProductV1.from_mapping(result.normalized_product)
        assert len(contract.images) == 1 and contract.images[0].role == "MAIN"
        assert len(image_records) == 1
        assert len(images.downloaded) == 2 and images.discarded == [images.downloaded[1]]
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retryable", "max_attempts", "expected"),
    [(True, 3, "RETRY"), (False, 3, "FAILED"), (True, 1, "FAILED")],
)
async def test_worker_classifies_failure_and_persists_only_stable_diagnostic(
    tmp_path: Path,
    retryable: bool,
    max_attempts: int,
    expected: str,
) -> None:
    engine, factory = await _collector_database(tmp_path)
    job_id = await _add_job(factory, max_attempts=max_attempts)

    async def collect(request: SourceRequest) -> SourceArtifact:
        raise SourceAdapterError(
            "source_unavailable" if retryable else "invalid_source_product",
            "upstream leaked token=super-secret buyer@example.test",
            retryable=retryable,
        )

    worker = CollectorWorker(
        session_factory=factory,
        registry=build_source_registry((_FakeAdapter(collect),)),
        images=_FakeImages(),  # type: ignore[arg-type]
        worker_id="worker-1",
        clock=lambda: _NOW,
    )
    try:
        outcome = (await worker.run_once(limit=1))[0]
        assert outcome.status == expected
        async with factory() as session:
            job = await session.get(CollectorJob, job_id)
            attempt = await session.scalar(
                select(CollectorAttempt).where(CollectorAttempt.collector_job_id == job_id)
            )
        assert job is not None and job.status == expected
        if expected == "RETRY":
            assert job.next_attempt_at > _NOW.replace(tzinfo=None)
        assert attempt is not None
        assert attempt.error_code == ("source_unavailable" if retryable else "invalid_source_product")
        assert attempt.error_redacted == "collector operation failed"
        assert "secret" not in attempt.error_redacted
        assert "example" not in attempt.error_redacted
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_stale_worker_cannot_overwrite_reassigned_lease(tmp_path: Path) -> None:
    engine, factory = await _collector_database(tmp_path)
    job_id = await _add_job(factory)

    async def collect(request: SourceRequest) -> SourceArtifact:
        async with factory() as session:
            job = await session.get(CollectorJob, job_id)
            assert job is not None
            job.lease_owner = "worker-2"
            job.lease_until = _NOW + timedelta(minutes=5)
            await session.commit()
        return _cj_artifact()

    images = _FakeImages()
    worker = CollectorWorker(
        session_factory=factory,
        registry=build_source_registry((_FakeAdapter(collect),)),
        images=images,  # type: ignore[arg-type]
        worker_id="worker-1",
        clock=lambda: _NOW,
    )
    try:
        outcome = (await worker.run_once(limit=1))[0]
        assert outcome.status == "LEASE_LOST"
        assert images.discarded == images.downloaded
        async with factory() as session:
            job = await session.get(CollectorJob, job_id)
            result = await session.scalar(
                select(CollectorResult).where(CollectorResult.collector_job_id == job_id)
            )
        assert job is not None and job.lease_owner == "worker-2" and job.status == "RUNNING"
        assert result is None
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_worker_fails_closed_when_external_work_outlives_its_lease(tmp_path: Path) -> None:
    engine, factory = await _collector_database(tmp_path)
    job_id = await _add_job(factory)
    current = _NOW

    async def collect(request: SourceRequest) -> SourceArtifact:
        nonlocal current
        current += timedelta(seconds=91)
        return _cj_artifact()

    images = _FakeImages()
    worker = CollectorWorker(
        session_factory=factory,
        registry=build_source_registry((_FakeAdapter(collect),)),
        images=images,  # type: ignore[arg-type]
        worker_id="worker-1",
        clock=lambda: current,
        lease_seconds=90,
    )
    try:
        outcome = (await worker.run_once(limit=1))[0]
        assert outcome.status == "LEASE_LOST"
        assert not images.downloaded
        async with factory() as session:
            reclaimed = await claim_due_jobs(
                session,
                worker_id="worker-2",
                limit=1,
                lease_seconds=90,
                now=current,
            )
            await session.commit()
        assert len(reclaimed) == 1 and reclaimed[0].id == job_id
        assert reclaimed[0].lease_owner == "worker-2" and reclaimed[0].attempts == 2
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_expired_running_lease_is_reclaimed_and_closes_previous_attempt(tmp_path: Path) -> None:
    engine, factory = await _collector_database(tmp_path)
    job_id = await _add_job(factory, max_attempts=3)
    try:
        async with factory() as session:
            first = await claim_due_jobs(
                session,
                worker_id="worker-1",
                limit=1,
                lease_seconds=10,
                now=_NOW,
            )
            await start_attempt(session, first[0], worker_id="worker-1", now=_NOW)
            await session.commit()
        recovery_time = _NOW + timedelta(seconds=10)
        async with factory() as session:
            second = await claim_due_jobs(
                session,
                worker_id="worker-2",
                limit=1,
                lease_seconds=20,
                now=recovery_time,
            )
            assert len(second) == 1 and second[0].attempts == 2
            await start_attempt(session, second[0], worker_id="worker-2", now=recovery_time)
            await session.commit()
        async with factory() as session:
            job = await session.get(CollectorJob, job_id)
            attempts = tuple(
                await session.scalars(
                    select(CollectorAttempt)
                    .where(CollectorAttempt.collector_job_id == job_id)
                    .order_by(CollectorAttempt.attempt_number)
                )
            )
        assert job is not None and job.status == "RUNNING" and job.lease_owner == "worker-2"
        assert attempts[0].outcome == "LEASE_EXPIRED"
        assert attempts[0].error_code == "collector_lease_expired"
        assert attempts[0].finished_at is not None and attempts[1].finished_at is None
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_expired_final_attempt_fails_closed_instead_of_remaining_running(tmp_path: Path) -> None:
    engine, factory = await _collector_database(tmp_path)
    job_id = await _add_job(factory, max_attempts=1)
    try:
        async with factory() as session:
            claimed = await claim_due_jobs(
                session,
                worker_id="worker-1",
                limit=1,
                lease_seconds=10,
                now=_NOW,
            )
            await start_attempt(session, claimed[0], worker_id="worker-1", now=_NOW)
            await session.commit()
        async with factory() as session:
            assert not await claim_due_jobs(
                session,
                worker_id="worker-2",
                limit=1,
                now=_NOW + timedelta(seconds=10),
            )
            await session.commit()
        async with factory() as session:
            job = await session.get(CollectorJob, job_id)
            attempt = await session.scalar(
                select(CollectorAttempt).where(CollectorAttempt.collector_job_id == job_id)
            )
        assert job is not None and job.status == "FAILED" and job.lease_owner is None
        assert attempt is not None and attempt.outcome == "FAILED"
        assert attempt.error_code == "collector_lease_expired"
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_repository_rejects_terminal_write_after_lease_expiry(tmp_path: Path) -> None:
    engine, factory = await _collector_database(tmp_path)
    await _add_job(factory)
    try:
        async with factory() as session:
            claimed = await claim_due_jobs(
                session,
                worker_id="worker-1",
                now=_NOW,
                lease_seconds=10,
            )
            await start_attempt(session, claimed[0], worker_id="worker-1", now=_NOW)
            await session.commit()
        async with factory() as session:
            job = await session.get(CollectorJob, claimed[0].id)
            assert job is not None
            with pytest.raises(CollectorLeaseLost):
                await persist_failure(
                    session,
                    job=job,
                    worker_id="worker-1",
                    error_code="source_timeout",
                    retryable=True,
                    now=_NOW + timedelta(seconds=10),
                )
            await session.rollback()
    finally:
        await engine.dispose()  # type: ignore[union-attr]


def _png(width: int = 1, height: int = 1) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return len(data).to_bytes(4, "big") + kind + data + crc.to_bytes(4, "big")

    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    pixels = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


async def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


@pytest.mark.asyncio
async def test_image_downloader_validates_container_and_stores_atomically() -> None:
    content = _png()
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=content)

    client = SafeHttpClient(
        OutboundPolicy(
            allowed_hosts=frozenset({"cf.cjdropshipping.com"}),
            max_response_bytes=5 * 1024 * 1024,
        ),
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    downloader = ImageDownloader({"CJ": client})
    image = await downloader.download(source="CJ", url="https://cf.cjdropshipping.com/product.png")
    try:
        absolute = resolve_collector_image_path(PROJECT_ROOT / image.relative_path)
        assert absolute.parent == COLLECTOR_IMAGE_DIR.resolve()
        assert absolute.read_bytes() == content
        assert image.width == 1 and image.height == 1
        assert image.sha256 and len(image.sha256) == 64
        assert seen[0].url.host == "93.184.216.34"
        assert not tuple(COLLECTOR_IMAGE_DIR.glob("*.part"))
    finally:
        assert await downloader.discard(image)


@pytest.mark.asyncio
async def test_image_downloader_applies_explicit_crop_and_watermark_policy() -> None:
    source = _png(width=8, height=4)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=source)

    client = SafeHttpClient(
        OutboundPolicy(allowed_hosts=frozenset({"cf.cjdropshipping.com"})),
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    downloader = ImageDownloader(
        {"CJ": client},
        transform=ImageTransformPolicy(center_square_crop=True, watermark_text="shop"),
    )
    image = await downloader.download(source="CJ", url="https://cf.cjdropshipping.com/product.png")
    try:
        stored = resolve_collector_image_path(PROJECT_ROOT / image.relative_path).read_bytes()
        assert stored != source
        assert inspect_image(stored) == ("image/png", 4, 4)
        assert image.width == 4 and image.height == 4
        assert image.sha256 == hashlib.sha256(stored).hexdigest()
    finally:
        assert await downloader.discard(image)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared", "content", "code"),
    [
        ("image/png", b"<html>not an image</html>", "image_format_invalid"),
        ("image/svg+xml", _png(), "image_type_mismatch"),
        ("image/png", _png() + b"<script>polyglot</script>", "image_container_invalid"),
    ],
)
async def test_image_downloader_rejects_disguised_or_ambiguous_content(
    declared: str,
    content: bytes,
    code: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": declared}, content=content)

    downloader = ImageDownloader(
        {
            "CJ": SafeHttpClient(
                OutboundPolicy(allowed_hosts=frozenset({"cf.cjdropshipping.com"})),
                resolver=_public_resolver,
                transport=httpx.MockTransport(handler),
            )
        }
    )
    with pytest.raises(SourceAdapterError) as captured:
        await downloader.download(source="CJ", url="https://cf.cjdropshipping.com/product.png")
    assert captured.value.code == code


def test_image_inspection_limits_dimensions_size_and_collector_paths() -> None:
    with pytest.raises(SourceAdapterError) as dimensions:
        inspect_image(_png(width=10_001, height=5_000))
    assert dimensions.value.code == "image_dimensions_invalid"
    with pytest.raises(SourceAdapterError) as size:
        inspect_image(b"x" * (5 * 1024 * 1024 + 1))
    assert size.value.code == "image_size_invalid"
    with pytest.raises(InvalidImageError):
        resolve_collector_image_path(COLLECTOR_IMAGE_DIR / ".." / f"{uuid4()}.png")


def test_1688_open_normalizer_maps_documented_product_shape() -> None:
    document = {
        "productInfo": {
            "productID": 123456789,
            "subject": "Official 1688 Product",
            "description": "<b>Useful</b> product",
            "categoryID": 1048182,
            "image": {
                "images": [
                    "img/ibank/2024/123/product-main.jpg",
                    "https://cbu02.alicdn.com/img/ibank/2024/123/product-detail.png",
                ]
            },
            "skuInfos": [
                {
                    "skuId": 4469920756190,
                    "price": "18.20",
                    "attributes": [{"attributeID": 321, "attValueID": 654}],
                }
            ],
            "saleInfo": {"priceRanges": [{"startQuantity": 1, "price": "18.20"}]},
        }
    }
    normalized = normalize_artifact(
        SourceArtifact(
            source="1688",
            mode=SourceMode.OFFICIAL_API,
            canonical_url="https://detail.1688.com/offer/123456789.html",
            source_product_id="123456789",
            media_type="application/json",
            body=json.dumps(document).encode(),
        )
    )

    assert normalized.source_product_id == "123456789"
    assert normalized.product.title == "Official 1688 Product"
    assert normalized.product.description == "Useful product"
    assert normalized.product.category_id == "1048182"
    assert normalized.product.skus[0].price == Decimal("18.20")
    assert normalized.product.skus[0].attributes == {"321": "654"}
    assert normalized.product.images[0].source_url == (
        "https://cbu01.alicdn.com/img/ibank/2024/123/product-main.jpg"
    )
    assert normalized.product.images[1].role == "DETAIL"
    assert normalized.product.source_trace["skus"] == "1688.productInfo.skuInfos"


def test_normalizers_reject_malicious_or_unsupported_source_documents() -> None:
    artifact = _cj_artifact()
    normalized = normalize_artifact(artifact)
    assert normalized.product.description == "Useful item"
    assert normalized.product.skus[0].price.as_tuple().exponent == -2

    malformed = SourceArtifact(
        source="CJ",
        mode=SourceMode.OFFICIAL_API,
        canonical_url=artifact.canonical_url,
        source_product_id="CJ12345",
        media_type="application/json",
        body=b'{"data":{"pid":"CJ12345","productNameEn":"x","variants":"not-array"}}',
    )
    with pytest.raises(SourceAdapterError) as invalid:
        normalize_artifact(malformed)
    assert invalid.value.code == "invalid_source_product"

    html = SourceArtifact(
        source="1688",
        mode=SourceMode.PUBLIC_PAGE,
        canonical_url="https://detail.1688.com/offer/123456789.html",
        source_product_id="123456789",
        media_type="text/html",
        body=b"<html><script>window.secret='cookie'</script></html>",
    )
    with pytest.raises(SourceAdapterError) as unsupported:
        normalize_artifact(html)
    assert unsupported.value.code == "source_layout_unsupported"


def test_1688_normalizer_accepts_only_explicit_product_json_ld() -> None:
    document = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Public Product",
        "description": "<b>Visible</b> description",
        "image": ["https://cbu01.alicdn.com/item.png"],
        "offers": {"price": "18.20", "priceCurrency": "CNY", "sku": "1688-SKU-1"},
    }
    body = (
        '<html><script type="application/ld+json">'
        + json.dumps(document)
        + "</script></html>"
    ).encode()
    normalized = normalize_artifact(
        SourceArtifact(
            source="1688",
            mode=SourceMode.PUBLIC_PAGE,
            canonical_url="https://detail.1688.com/offer/123456789.html",
            source_product_id="123456789",
            media_type="text/html",
            body=body,
        )
    )
    assert normalized.source_product_id == "123456789"
    assert normalized.product.title == "Public Product"
    assert normalized.product.skus[0].currency == "CNY"
    assert "public_page_facts_require_human_confirmation" in normalized.product.unmapped_warnings