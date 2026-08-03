"""Core-owned consumer for the versioned Collector import contract."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import CollectorImportReceipt, ShopBinding
from app.domain.product import NormalizedImage, NormalizedProduct, NormalizedSku
from app.domain.product_payload import normalized_product_to_payload
from app.repositories.catalog import save_product_draft
from app.repositories.idempotency import canonical_payload_hash
from shared.collector_contract import (
    CONTRACT_VERSION,
    CollectorContractError,
    CollectorImportEnvelopeV1,
    CollectorImportReceiptV1,
)


class CollectorImportConflict(RuntimeError):
    pass


async def import_collector_product(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    envelope: CollectorImportEnvelopeV1,
) -> CollectorImportReceiptV1:
    """Idempotently turn one validated envelope into a Core draft.

    The caller owns the Core transaction. This function never reads the
    Collector database or filesystem and stores only opaque image-record IDs.
    """

    selected_shop = shop_binding_id.strip()
    if not selected_shop:
        raise CollectorContractError("shop binding is required")
    shop = await session.get(ShopBinding, selected_shop)
    if shop is None:
        raise LookupError("target shop binding was not found")

    product = _to_core_product(envelope)
    payload_hash = canonical_payload_hash(normalized_product_to_payload(product))
    existing = await session.scalar(
        select(CollectorImportReceipt).where(CollectorImportReceipt.result_id == envelope.result_id)
    )
    if existing is not None:
        if (
            existing.shop_binding_id != selected_shop
            or existing.job_id != envelope.job_id
            or existing.source != envelope.source
            or existing.source_mode != envelope.source_mode
            or existing.source_product_id != envelope.source_product_id
            or existing.envelope_digest != envelope.digest
            or existing.payload_hash != payload_hash
            or existing.contract_version != CONTRACT_VERSION
        ):
            raise CollectorImportConflict("collector result was replayed with different facts")
        return CollectorImportReceiptV1(
            result_id=envelope.result_id,
            draft_id=existing.product_draft_id,
            envelope_digest=existing.envelope_digest,
            created=False,
        )

    draft, _draft_created = await save_product_draft(
        session,
        shop_binding_id=selected_shop,
        product=product,
        source_kind="COLLECTOR",
        source_result_id=envelope.result_id,
        field_sources=envelope.product.source_trace,
    )
    if envelope.digest is None:
        raise CollectorContractError("import envelope has no integrity digest")
    receipt = CollectorImportReceipt(
        shop_binding_id=selected_shop,
        result_id=envelope.result_id,
        job_id=envelope.job_id,
        source=envelope.source,
        source_mode=envelope.source_mode,
        source_product_id=envelope.source_product_id,
        envelope_digest=envelope.digest,
        payload_hash=payload_hash,
        product_draft_id=draft.id,
        contract_version=CONTRACT_VERSION,
    )
    session.add(receipt)
    await session.flush()
    return CollectorImportReceiptV1(
        result_id=envelope.result_id,
        draft_id=draft.id,
        envelope_digest=envelope.digest,
        created=True,
    )


class CoreCollectorImportService:
    """Core-side port implementation that owns only the Core transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def import_product(
        self,
        *,
        shop_binding_id: str,
        envelope: CollectorImportEnvelopeV1,
    ) -> CollectorImportReceiptV1:
        async with self._session_factory() as session:
            receipt = await import_collector_product(
                session,
                shop_binding_id=shop_binding_id,
                envelope=envelope,
            )
            await session.commit()
            return receipt


def _to_core_product(envelope: CollectorImportEnvelopeV1) -> NormalizedProduct:
    contract = envelope.product
    return NormalizedProduct(
        title=contract.title,
        description=contract.description,
        category_id=contract.category_id,
        skus=tuple(
            NormalizedSku(
                seller_sku=item.seller_sku,
                price=item.price,
                currency=item.currency,
                inventory_by_warehouse={},
                attributes=item.attributes,
            )
            for item in contract.skus
        ),
        images=tuple(
            NormalizedImage(
                source_url=f"collector-image:{item.image_record_id}",
                role=item.role,
            )
            for item in contract.images
        ),
        attributes=contract.attributes,
        source_trace=contract.source_trace,
        unmapped_warnings=contract.unmapped_warnings,
    )