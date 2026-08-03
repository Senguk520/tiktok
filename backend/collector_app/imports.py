"""Collector-owned export and idempotent handoff orchestration."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from collector_app.db.models import CollectorJob, CollectorResult, ImageRecord
from collector_app.db.repository import mark_result_imported
from shared.collector_contract import (
    CollectorContractError,
    CollectorImportEnvelopeV1,
    CollectorImportReceiptV1,
    CollectorProductV1,
)


@runtime_checkable
class CoreImportPort(Protocol):
    async def import_product(
        self,
        *,
        shop_binding_id: str,
        envelope: CollectorImportEnvelopeV1,
    ) -> CollectorImportReceiptV1: ...


async def export_result(
    session: AsyncSession,
    *,
    result_id: str,
) -> CollectorImportEnvelopeV1:
    """Export normalized facts only; raw artifacts and image paths stay local."""

    row = await session.execute(
        select(CollectorResult, CollectorJob)
        .join(CollectorJob, CollectorJob.id == CollectorResult.collector_job_id)
        .where(CollectorResult.id == result_id)
    )
    pair = row.one_or_none()
    if pair is None:
        raise LookupError("collector result was not found")
    result, job = pair
    if job.status != "SUCCEEDED":
        raise CollectorContractError("collector result does not belong to a successful job")
    if not result.source_product_id:
        raise CollectorContractError("collector result has no source product identity")
    product = CollectorProductV1.from_mapping(result.normalized_product)
    image_ids = tuple(item.image_record_id for item in product.images)
    records = tuple(
        await session.scalars(select(ImageRecord).where(ImageRecord.id.in_(image_ids)))
    )
    available = {
        record.id: record
        for record in records
        if record.ready and record.deleted_at is None and record.relative_path
    }
    if set(available) != set(image_ids):
        raise CollectorContractError("collector result references unavailable images")
    for image in product.images:
        record = available[image.image_record_id]
        if (
            record.sha256 != image.sha256
            or record.content_type != image.content_type
            or record.byte_size != image.byte_size
            or record.width != image.width
            or record.height != image.height
        ):
            raise CollectorContractError("collector image metadata changed after normalization")
    return CollectorImportEnvelopeV1(
        result_id=result.id,
        job_id=job.id,
        source=job.source,
        source_mode=job.source_mode,
        source_product_id=result.source_product_id,
        product=product,
    )


class CollectorImportCoordinator:
    """Bridge two independent transactions through a replay-safe Core port."""

    def __init__(
        self,
        *,
        collector_session_factory: async_sessionmaker[AsyncSession],
        core_port: CoreImportPort,
    ) -> None:
        if not isinstance(core_port, CoreImportPort):
            raise TypeError("core import port does not implement the contract")
        self._collector_session_factory = collector_session_factory
        self._core_port = core_port

    async def import_result(
        self,
        *,
        result_id: str,
        shop_binding_id: str,
    ) -> CollectorImportReceiptV1:
        async with self._collector_session_factory() as session:
            envelope = await export_result(session, result_id=result_id)
        receipt = await self._core_port.import_product(
            shop_binding_id=shop_binding_id,
            envelope=envelope,
        )
        if (
            receipt.result_id != envelope.result_id
            or receipt.envelope_digest != envelope.digest
        ):
            raise CollectorContractError("Core import receipt does not match the exported envelope")
        async with self._collector_session_factory() as session:
            await mark_result_imported(session, result_id=result_id)
            await session.commit()
        return receipt