from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import DatabaseSettings, create_engine_and_session_factory
from app.db.models import CollectorImportReceipt, ProductDraft, ShopBinding
from app.use_cases.collector_imports import (
    CollectorImportConflict,
    CoreCollectorImportService,
    import_collector_product,
)
from collector_app.db.base import CollectorDatabaseSettings
from collector_app.db.base import create_engine_and_session_factory as collector_factory
from collector_app.db.models import CollectorJob, CollectorResult, ImageRecord
from collector_app.imports import CollectorImportCoordinator, export_result
from migrations.collector import migrate_engine as migrate_collector
from migrations.core import migrate_engine as migrate_core
from shared.collector_contract import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    CollectorContractError,
    CollectorImageV1,
    CollectorImportEnvelopeV1,
    CollectorImportReceiptV1,
    CollectorProductV1,
    CollectorSkuV1,
)


async def _core_database() -> tuple[object, async_sessionmaker[AsyncSession]]:
    engine, factory = create_engine_and_session_factory(
        DatabaseSettings(url="sqlite+aiosqlite:///:memory:", path=None)
    )
    await migrate_core(engine)
    return engine, factory


async def _collector_database(
    tmp_path: Path,
) -> tuple[object, async_sessionmaker[AsyncSession]]:
    path = tmp_path / f"collector-import-{uuid4()}.sqlite3"
    engine, factory = collector_factory(
        CollectorDatabaseSettings(url=f"sqlite+aiosqlite:///{path.as_posix()}", path=path)
    )
    await migrate_collector(engine)
    return engine, factory


def _product() -> CollectorProductV1:
    return CollectorProductV1(
        title="Imported Product",
        description="normalized facts only",
        category_id=None,
        skus=(
            CollectorSkuV1(
                seller_sku="SOURCE-SKU-1",
                price=Decimal("12.50"),
                currency="USD",
                attributes={"source_variant": "Black"},
            ),
        ),
        images=(
            CollectorImageV1(
                image_record_id="image-record-1",
                role="MAIN",
                sha256="a" * 64,
                content_type="image/png",
                byte_size=67,
                width=1,
                height=1,
            ),
        ),
        attributes={},
        source_trace={"title": "CJ.productNameEn"},
        unmapped_warnings=("tiktok_category_requires_manual_mapping",),
    )


def _envelope(*, result_id: str = "collector-result-1") -> CollectorImportEnvelopeV1:
    return CollectorImportEnvelopeV1(
        result_id=result_id,
        job_id="collector-job-1",
        source="CJ",
        source_mode="OFFICIAL_API",
        source_product_id="CJ12345",
        product=_product(),
    )


@pytest.mark.asyncio
async def test_core_import_is_versioned_idempotent_and_stores_no_collector_path() -> None:
    engine, factory = await _core_database()
    try:
        async with factory() as session:
            shop = ShopBinding(open_id="owner", shop_id="shop-1", region="MY")
            session.add(shop)
            await session.commit()
            shop_id = shop.id

        service = CoreCollectorImportService(factory)
        first = await service.import_product(shop_binding_id=shop_id, envelope=_envelope())
        second = await service.import_product(shop_binding_id=shop_id, envelope=_envelope())
        assert first.created and not second.created and first.draft_id == second.draft_id

        async with factory() as session:
            draft_count = await session.scalar(select(func.count()).select_from(ProductDraft))
            receipt_count = await session.scalar(
                select(func.count()).select_from(CollectorImportReceipt)
            )
            receipt = await session.scalar(select(CollectorImportReceipt))
            draft = await session.get(ProductDraft, first.draft_id)
        assert draft_count == 1 and receipt_count == 1
        assert receipt is not None
        assert receipt.job_id == "collector-job-1"
        assert receipt.source_mode == "OFFICIAL_API"
        assert receipt.envelope_digest == _envelope().digest == first.envelope_digest
        assert draft is not None and draft.source_kind == "COLLECTOR"
        assert draft.normalized_payload["images"] == [
            {
                "source_url": "collector-image:image-record-1",
                "role": "MAIN",
                "local_image_id": None,
            }
        ]
        rendered = str(draft.normalized_payload)
        assert "temp/images" not in rendered and "collector.sqlite" not in rendered
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_core_import_rejects_result_replay_with_different_facts() -> None:
    engine, factory = await _core_database()
    try:
        async with factory() as session:
            shop = ShopBinding(open_id="owner", shop_id="shop-1", region="MY")
            session.add(shop)
            await session.flush()
            await import_collector_product(session, shop_binding_id=shop.id, envelope=_envelope())
            await session.commit()
            shop_id = shop.id
        changed = CollectorImportEnvelopeV1(
            result_id="collector-result-1",
            job_id="collector-job-1",
            source="CJ",
            source_mode="OFFICIAL_API",
            source_product_id="DIFFERENT",
            product=_product(),
        )
        async with factory() as session:
            with pytest.raises(CollectorImportConflict):
                await import_collector_product(session, shop_binding_id=shop_id, envelope=changed)
    finally:
        await engine.dispose()  # type: ignore[union-attr]


def test_contract_rejects_unknown_fields_and_versions() -> None:
    raw = _envelope().to_mapping()
    assert raw["contract"] == CONTRACT_NAME and raw["version"] == CONTRACT_VERSION
    assert CollectorImportEnvelopeV1.from_mapping(raw) == _envelope()

    unknown = dict(raw)
    unknown["raw_upstream_body"] = "secret"
    with pytest.raises(CollectorContractError, match="fields"):
        CollectorImportEnvelopeV1.from_mapping(unknown)

    unsupported = dict(raw)
    unsupported["version"] = 999
    with pytest.raises(CollectorContractError, match="unsupported"):
        CollectorImportEnvelopeV1.from_mapping(unsupported)

    tampered = dict(raw)
    tampered_product = dict(raw["product"])
    tampered_product["title"] = "Changed after export"
    tampered["product"] = tampered_product
    with pytest.raises(CollectorContractError, match="digest"):
        CollectorImportEnvelopeV1.from_mapping(tampered)

    official_1688 = CollectorImportEnvelopeV1(
        result_id="collector-result-1688",
        job_id="collector-job-1688",
        source="1688",
        source_mode="OFFICIAL_API",
        source_product_id="123456789",
        product=_product(),
    )
    assert CollectorImportEnvelopeV1.from_mapping(official_1688.to_mapping()) == official_1688

    wrong_mode = dict(raw)
    wrong_mode["source_mode"] = "PUBLIC_PAGE"
    with pytest.raises(CollectorContractError, match="source identity"):
        CollectorImportEnvelopeV1.from_mapping(wrong_mode)


async def _seed_collector_result(
    factory: async_sessionmaker[AsyncSession],
    *,
    result_id: str = "collector-result-1",
) -> None:
    product = _product()
    async with factory() as session:
        job = CollectorJob(
            id="collector-job-1",
            source="CJ",
            source_mode="OFFICIAL_API",
            source_url="https://www.cjdropshipping.com/product/item.html?pid=CJ12345",
            request_payload={},
            request_hash=uuid4().hex.ljust(64, "0")[:64],
            status="SUCCEEDED",
            next_attempt_at=datetime.now(UTC),
        )
        session.add(job)
        await session.flush()
        session.add(
            ImageRecord(
                id="image-record-1",
                collector_job_id=job.id,
                relative_path="temp/images/collector/00000000-0000-4000-8000-000000000001.png",
                sha256="a" * 64,
                content_type="image/png",
                byte_size=67,
                width=1,
                height=1,
                source_url_redacted="https://cf.cjdropshipping.com/image.png",
                ready=True,
            )
        )
        session.add(
            CollectorResult(
                id=result_id,
                collector_job_id=job.id,
                source_product_id="CJ12345",
                normalized_product=product.to_mapping(),
                field_sources=dict(product.source_trace),
                unmapped_warnings=list(product.unmapped_warnings),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_export_rejects_nonterminal_job_and_changed_image_metadata(tmp_path: Path) -> None:
    collector_engine, collector_sessions = await _collector_database(tmp_path)
    try:
        await _seed_collector_result(collector_sessions)
        async with collector_sessions() as session:
            job = await session.get(CollectorJob, "collector-job-1")
            assert job is not None
            job.status = "RUNNING"
            await session.commit()
        async with collector_sessions() as session:
            with pytest.raises(CollectorContractError, match="successful"):
                await export_result(session, result_id="collector-result-1")
        async with collector_sessions() as session:
            job = await session.get(CollectorJob, "collector-job-1")
            image = await session.get(ImageRecord, "image-record-1")
            assert job is not None and image is not None
            job.status = "SUCCEEDED"
            image.byte_size = 68
            await session.commit()
        async with collector_sessions() as session:
            with pytest.raises(CollectorContractError, match="metadata"):
                await export_result(session, result_id="collector-result-1")
    finally:
        await collector_engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_import_coordinator_keeps_database_transactions_separate_and_replay_safe(
    tmp_path: Path,
) -> None:
    collector_engine, collector_sessions = await _collector_database(tmp_path)
    core_engine, core_sessions = await _core_database()
    try:
        await _seed_collector_result(collector_sessions)
        async with core_sessions() as session:
            shop = ShopBinding(open_id="owner", shop_id="shop-1", region="MY")
            session.add(shop)
            await session.commit()
            shop_id = shop.id

        coordinator = CollectorImportCoordinator(
            collector_session_factory=collector_sessions,
            core_port=CoreCollectorImportService(core_sessions),
        )
        first = await coordinator.import_result(
            result_id="collector-result-1",
            shop_binding_id=shop_id,
        )
        second = await coordinator.import_result(
            result_id="collector-result-1",
            shop_binding_id=shop_id,
        )
        assert first.created and not second.created and first.draft_id == second.draft_id

        async with collector_sessions() as session:
            result = await session.get(CollectorResult, "collector-result-1")
            envelope = await export_result(session, result_id="collector-result-1")
        assert result is not None and result.imported_at is not None
        exported = str(envelope.to_mapping())
        assert "temp/images" not in exported and "cf.cjdropshipping.com" not in exported
    finally:
        await collector_engine.dispose()  # type: ignore[union-attr]
        await core_engine.dispose()  # type: ignore[union-attr]


class _FailingCorePort:
    async def import_product(
        self,
        *,
        shop_binding_id: str,
        envelope: CollectorImportEnvelopeV1,
    ) -> CollectorImportReceiptV1:
        raise RuntimeError("core unavailable")


@pytest.mark.asyncio
async def test_collector_marks_imported_only_after_core_receipt(tmp_path: Path) -> None:
    collector_engine, collector_sessions = await _collector_database(tmp_path)
    try:
        await _seed_collector_result(collector_sessions)
        coordinator = CollectorImportCoordinator(
            collector_session_factory=collector_sessions,
            core_port=_FailingCorePort(),
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            await coordinator.import_result(
                result_id="collector-result-1",
                shop_binding_id="shop-1",
            )
        async with collector_sessions() as session:
            result = await session.get(CollectorResult, "collector-result-1")
        assert result is not None and result.imported_at is None
    finally:
        await collector_engine.dispose()  # type: ignore[union-attr]