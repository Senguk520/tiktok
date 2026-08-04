from __future__ import annotations

import hashlib
import io
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import DatabaseSettings, create_engine_and_session_factory
from app.db.models import CollectorImportReceipt, ProductDraft, ShopBinding
from app.integrations.collector import (
    COLLECTOR_BASE_URL,
    CollectorClientError,
    CollectorClientSettings,
    CollectorHttpClient,
)
from app.use_cases.collector_imports import CoreCollectorImportService
from collector_app.api import install_collector_api
from collector_app.db.base import CollectorDatabaseSettings
from collector_app.db.base import create_engine_and_session_factory as collector_factory
from collector_app.db.models import CollectorJob, CollectorResult, ImageRecord
from collector_app.main import create_app as create_collector_app
from migrations.collector import migrate_engine as migrate_collector
from migrations.core import migrate_engine as migrate_core
from shared.collector_contract import (
    CollectorImageV1,
    CollectorImportReceiptV1,
    CollectorProductV1,
    CollectorSkuV1,
)
from shared.http_security import install_security_middleware
from shared.safe_paths import COLLECTOR_IMAGE_DIR, PROJECT_ROOT, ensure_runtime_directories
from shared.security import (
    INTERNAL_HMAC_SIGNATURE_HEADER,
    INTERNAL_HMAC_TIMESTAMP_HEADER,
    sign_internal_message,
    utc_timestamp,
)

_SECRET = b"collector-boundary-test-secret-32-bytes-minimum"


async def _collector_database(
    tmp_path: Path,
) -> tuple[object, async_sessionmaker[AsyncSession]]:
    path = tmp_path / f"collector-http-{uuid4()}.sqlite3"
    engine, factory = collector_factory(
        CollectorDatabaseSettings(url=f"sqlite+aiosqlite:///{path.as_posix()}", path=path)
    )
    await migrate_collector(engine)
    return engine, factory


async def _core_database() -> tuple[object, async_sessionmaker[AsyncSession]]:
    engine, factory = create_engine_and_session_factory(
        DatabaseSettings(url="sqlite+aiosqlite:///:memory:", path=None)
    )
    await migrate_core(engine)
    return engine, factory


def _internal_app(
    factory: async_sessionmaker[AsyncSession],
    *,
    secret: bytes | None = _SECRET,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_security_middleware(app)
    install_collector_api(app)
    app.state.db_session_factory = factory
    app.state.internal_hmac_secret = secret
    return app


def _transport(app: FastAPI) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app, client=("127.0.0.1", 32100))


def _body(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _signed_headers(
    *,
    method: str,
    path: str,
    body: bytes = b"",
    timestamp: int | None = None,
) -> dict[str, str]:
    selected = utc_timestamp() if timestamp is None else timestamp
    return {
        "Content-Type": "application/json",
        INTERNAL_HMAC_TIMESTAMP_HEADER: str(selected),
        INTERNAL_HMAC_SIGNATURE_HEADER: sign_internal_message(
            _SECRET,
            timestamp=selected,
            method=method,
            path=path,
            body=body,
        ),
    }


def _png() -> bytes:
    target = io.BytesIO()
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(target, format="PNG")
    return target.getvalue()


async def _seed_success_result(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[str, str, str, Path, bytes]:
    ensure_runtime_directories()
    job_id = str(uuid4())
    result_id = str(uuid4())
    image_id = str(uuid4())
    image_path = COLLECTOR_IMAGE_DIR / f"{uuid4()}.png"
    content = _png()
    image_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    product = CollectorProductV1(
        title="HTTP Imported Product",
        description="strict versioned facts",
        category_id=None,
        skus=(
            CollectorSkuV1(
                seller_sku="HTTP-SKU-1",
                price=Decimal("12.50"),
                currency="USD",
                attributes={"variant": "red"},
            ),
        ),
        images=(
            CollectorImageV1(
                image_record_id=image_id,
                role="MAIN",
                sha256=digest,
                content_type="image/png",
                byte_size=len(content),
                width=1,
                height=1,
            ),
        ),
        attributes={},
        source_trace={"title": "CJ.productNameEn"},
        unmapped_warnings=(),
    )
    async with factory() as session:
        session.add(
            CollectorJob(
                id=job_id,
                source="CJ",
                source_mode="OFFICIAL_API",
                source_url="https://www.cjdropshipping.com/product/item.html?pid=CJHTTP1",
                request_payload={},
                request_hash=uuid4().hex.ljust(64, "0")[:64],
                status="SUCCEEDED",
                next_attempt_at=datetime.now(UTC),
            )
        )
        await session.flush()
        session.add(
            ImageRecord(
                id=image_id,
                collector_job_id=job_id,
                relative_path=image_path.relative_to(PROJECT_ROOT).as_posix(),
                sha256=digest,
                content_type="image/png",
                byte_size=len(content),
                width=1,
                height=1,
                ready=True,
            )
        )
        session.add(
            CollectorResult(
                id=result_id,
                collector_job_id=job_id,
                source_product_id="CJHTTP1",
                normalized_product=product.to_mapping(),
                field_sources=dict(product.source_trace),
                unmapped_warnings=[],
            )
        )
        await session.commit()
    return job_id, result_id, image_id, image_path, content


def test_core_collector_endpoint_is_fixed_loopback_and_not_configurable() -> None:
    assert COLLECTOR_BASE_URL == "http://127.0.0.1:8010"
    with pytest.raises(TypeError):
        CollectorClientSettings(
            secret=_SECRET,
            base_url="http://collector.invalid:8010",  # type: ignore[call-arg]
        )


def test_core_source_does_not_reference_collector_persistence() -> None:
    forbidden = (
        "collector_app.db",
        "collector_database_path",
        "collectordatabasesettings",
        "collector_session_factory",
    )
    violations: list[str] = []
    for source_path in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        source = source_path.read_text(encoding="utf-8").lower()
        if any(value in source for value in forbidden):
            violations.append(source_path.relative_to(PROJECT_ROOT).as_posix())
    assert violations == []


@pytest.mark.asyncio
async def test_signed_job_api_rejects_missing_stale_tampered_and_invalid_intents(
    tmp_path: Path,
) -> None:
    engine, factory = await _collector_database(tmp_path)
    app = _internal_app(factory)
    path = "/internal/v1/jobs"
    valid_payload = {
        "source": "CJ",
        "source_mode": "OFFICIAL_API",
        "source_url": "https://www.cjdropshipping.com/product/item.html?pid=CJ12345",
    }
    valid_body = _body(valid_payload)
    try:
        async with httpx.AsyncClient(
            transport=_transport(app),
            base_url=COLLECTOR_BASE_URL,
        ) as raw:
            unsigned = await raw.post(path, content=valid_body, headers={"Content-Type": "application/json"})
            assert unsigned.status_code == 401
            assert unsigned.json()["error"]["code"] == "INTERNAL_AUTHENTICATION_FAILED"

            stale = await raw.post(
                path,
                content=valid_body,
                headers=_signed_headers(
                    method="POST",
                    path=path,
                    body=valid_body,
                    timestamp=utc_timestamp() - 301,
                ),
            )
            assert stale.status_code == 401

            changed = _body({**valid_payload, "source_url": "https://evil.invalid/private"})
            tampered = await raw.post(
                path,
                content=changed,
                headers=_signed_headers(method="POST", path=path, body=valid_body),
            )
            assert tampered.status_code == 401

            extra = _body({**valid_payload, "collector_base_url": "http://evil.invalid"})
            strict = await raw.post(
                path,
                content=extra,
                headers=_signed_headers(method="POST", path=path, body=extra),
            )
            assert strict.status_code == 422
            assert strict.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
            assert "evil.invalid" not in strict.text

        client = CollectorHttpClient(
            CollectorClientSettings(secret=_SECRET),
            transport=_transport(app),
        )
        first = await client.create_job(**valid_payload)
        second = await client.create_job(**valid_payload)
        assert not first.reused and second.reused and first.job_id == second.job_id
        status = await client.get_job(first.job_id)
        assert status.job_id == first.job_id and status.status == "QUEUED"

        with pytest.raises(CollectorClientError) as wrong_pair:
            await client.create_job(
                source="CJ",
                source_mode="PUBLIC_PAGE",
                source_url=valid_payload["source_url"],
            )
        assert wrong_pair.value.status_code == 422

        with pytest.raises(CollectorClientError) as wrong_host:
            await client.create_job(
                source="1688",
                source_mode="PUBLIC_PAGE",
                source_url="https://127.0.0.1/offer/123456789.html",
            )
        assert wrong_host.value.status_code == 422

        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(CollectorJob))
        assert count == 1
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_collector_api_fails_closed_without_hmac_configuration(tmp_path: Path) -> None:
    engine, factory = await _collector_database(tmp_path)
    app = _internal_app(factory, secret=None)
    client = CollectorHttpClient(
        CollectorClientSettings(secret=_SECRET),
        transport=_transport(app),
    )
    try:
        with pytest.raises(CollectorClientError) as captured:
            await client.create_job(
                source="CJ",
                source_mode="OFFICIAL_API",
                source_url="https://www.cjdropshipping.com/product/item.html?pid=CJ12345",
            )
        assert captured.value.status_code == 503
        assert captured.value.code == "BLOCKED_CONFIGURATION"
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_export_image_import_and_receipt_ack_cross_only_http_boundary(tmp_path: Path) -> None:
    collector_engine, collector_sessions = await _collector_database(tmp_path)
    core_engine, core_sessions = await _core_database()
    _job_id, result_id, image_id, image_path, image_content = await _seed_success_result(
        collector_sessions
    )
    collector_app = _internal_app(collector_sessions)
    client = CollectorHttpClient(
        CollectorClientSettings(secret=_SECRET),
        transport=_transport(collector_app),
    )
    try:
        async with core_sessions() as session:
            shop = ShopBinding(open_id="owner", shop_id="shop-http", region="MY")
            session.add(shop)
            await session.commit()
            shop_id = shop.id

        envelope = await client.export_result(result_id)
        assert envelope.result_id == result_id
        assert str(envelope.to_mapping()).find("temp/images") == -1

        unknown_id = str(uuid4())
        with pytest.raises(CollectorClientError) as missing_image:
            await client.read_image(unknown_id)
        assert missing_image.value.status_code == 404

        image_path.write_bytes(image_content + b"tampered")
        with pytest.raises(CollectorClientError) as tampered_image:
            await client.read_image(image_id)
        assert tampered_image.value.status_code == 409
        image_path.write_bytes(image_content)
        image = await client.read_image(image_id)
        assert image.content_type == "image/png" and image.content == image_content

        rejected = CollectorImportReceiptV1(
            result_id=result_id,
            draft_id=str(uuid4()),
            envelope_digest="0" * 64,
            created=True,
        )
        with pytest.raises(CollectorClientError) as wrong_receipt:
            await client.acknowledge_receipt(rejected)
        assert wrong_receipt.value.status_code == 409
        async with collector_sessions() as session:
            collector_result = await session.get(CollectorResult, result_id)
            assert collector_result is not None and collector_result.imported_at is None

        service = CoreCollectorImportService(core_sessions)
        assert not hasattr(service, "_collector_session_factory")
        assert not hasattr(client, "_collector_session_factory")
        first_receipt = await service.import_product(
            shop_binding_id=shop_id,
            envelope=envelope,
        )
        first_ack = await client.acknowledge_receipt(first_receipt)
        second_receipt = await service.import_product(
            shop_binding_id=shop_id,
            envelope=envelope,
        )
        second_ack = await client.acknowledge_receipt(second_receipt)
        assert first_receipt.created and not second_receipt.created
        assert first_receipt.draft_id == second_receipt.draft_id
        assert first_ack.newly_marked and not second_ack.newly_marked

        async with collector_sessions() as session:
            imported = await session.get(CollectorResult, result_id)
        async with core_sessions() as session:
            draft_count = await session.scalar(select(func.count()).select_from(ProductDraft))
            receipt_count = await session.scalar(
                select(func.count()).select_from(CollectorImportReceipt)
            )
        assert imported is not None and imported.imported_at is not None
        assert draft_count == 1 and receipt_count == 1
    finally:
        if image_path.exists():
            image_path.unlink()
        await collector_engine.dispose()  # type: ignore[union-attr]
        await core_engine.dispose()  # type: ignore[union-attr]


def test_collector_lifespan_worker_polls_sqlite_and_fails_closed_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = PROJECT_ROOT / "data" / f"test-collector-worker-http-{uuid4()}.sqlite3"
    secret_text = _SECRET.decode("utf-8")
    monkeypatch.setenv("COLLECTOR_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("COLLECTOR_INTERNAL_HMAC_SECRET", secret_text)
    monkeypatch.delenv("CJ_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ALIBABA_1688_APP_KEY", raising=False)
    monkeypatch.delenv("ALIBABA_1688_APP_SECRET", raising=False)
    monkeypatch.delenv("ALIBABA_1688_ACCESS_TOKEN", raising=False)
    app = create_collector_app(start_worker=True)
    path = "/internal/v1/jobs"
    body = _body(
        {
            "source": "CJ",
            "source_mode": "OFFICIAL_API",
            "source_url": "https://www.cjdropshipping.com/product/item.html?pid=CJWORKER1",
        }
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert app.state.collector_worker is not None
            created = client.post(
                path,
                content=body,
                headers=_signed_headers(method="POST", path=path, body=body),
            )
            assert created.status_code == 201
            job_path = f"/internal/v1/jobs/{created.json()['job_id']}"
            deadline = time.monotonic() + 3
            status: dict[str, object] = {}
            while time.monotonic() < deadline:
                response = client.get(
                    job_path,
                    headers=_signed_headers(method="GET", path=job_path),
                )
                assert response.status_code == 200
                status = response.json()
                if status["status"] == "FAILED":
                    break
                time.sleep(0.05)
            assert status["status"] == "FAILED"
            assert status["error_code"] == "source_credentials_missing"
        assert app.state.collector_worker is None
    finally:
        for suffix in ("", "-wal", "-shm"):
            path_to_remove = Path(f"{database_path}{suffix}")
            if path_to_remove.exists():
                path_to_remove.unlink()