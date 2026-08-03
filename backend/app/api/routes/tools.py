"""Protected translation and Decimal profit tools."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import AuthenticatedAdmin, require_admin_session, require_csrf
from app.api.dependencies import ShopBindingId, commerce_runtime, database_session, session_factory
from app.api.errors import ERROR_RESPONSES, ApiProblem
from app.api.runtime import CommerceRuntime
from app.db.models import IdempotentOperation, ShopBinding
from app.domain.enums import WriteState
from app.integrations.translation import (
    TranslationRequest,
    TranslationRequestRejected,
    TranslationUpstreamError,
)
from app.repositories.audit import record_audit_fact
from app.repositories.idempotency import (
    IdempotencyRequest,
    canonical_payload_hash,
    register_operation,
)
from app.use_cases.profit import (
    ProfitEstimate,
    ProfitInputError,
    ProfitInputs,
    estimate_profit,
    profit_for_price,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolsCapabilitiesResponse(_StrictModel):
    translation_configured: bool
    translation_provider: str | None
    supported_translation_languages: list[str]
    translation_cache_enabled: bool
    blockers: list[str]


class TranslationInput(_StrictModel):
    texts: list[str] = Field(min_length=1, max_length=25)
    source_language: str = Field(min_length=2, max_length=16)
    target_language: str = Field(min_length=2, max_length=16)


class TranslationResponse(_StrictModel):
    texts: list[str]
    source_language: str
    target_language: str
    provider: str
    provider_request_id: str | None
    cached: bool


class ProfitInput(_StrictModel):
    product_cost: Decimal
    source_currency: str = Field(min_length=3, max_length=3)
    settlement_currency: str = Field(min_length=3, max_length=3)
    exchange_rate: Decimal
    shipping_cost: Decimal = Decimal("0")
    other_fixed_cost: Decimal = Decimal("0")
    commission_rate: Decimal
    payment_fee_rate: Decimal = Decimal("0")
    target_margin_rate: Decimal
    selling_price: Decimal | None = None


class ProfitResponse(_StrictModel):
    settlement_currency: str
    converted_product_cost: Decimal
    total_fixed_cost: Decimal
    suggested_price: Decimal
    commission_amount: Decimal
    payment_fee_amount: Decimal
    estimated_profit: Decimal
    realized_margin_rate: Decimal
    realized_margin_percent: Decimal
    exchange_rate: Decimal


router = APIRouter(
    prefix="/api/shops/{shop_binding_id}/tools",
    tags=["tools"],
    responses=ERROR_RESPONSES,
)


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and value else None


def _profit_inputs(payload: ProfitInput) -> ProfitInputs:
    return ProfitInputs(
        product_cost=payload.product_cost,
        source_currency=payload.source_currency,
        settlement_currency=payload.settlement_currency,
        exchange_rate=payload.exchange_rate,
        shipping_cost=payload.shipping_cost,
        other_fixed_cost=payload.other_fixed_cost,
        commission_rate=payload.commission_rate,
        payment_fee_rate=payload.payment_fee_rate,
        target_margin_rate=payload.target_margin_rate,
    )


def _profit_response(result: ProfitEstimate) -> ProfitResponse:
    return ProfitResponse(
        settlement_currency=result.settlement_currency,
        converted_product_cost=result.converted_product_cost,
        total_fixed_cost=result.total_fixed_cost,
        suggested_price=result.suggested_price,
        commission_amount=result.commission_amount,
        payment_fee_amount=result.payment_fee_amount,
        estimated_profit=result.estimated_profit,
        realized_margin_rate=result.realized_margin_rate,
        realized_margin_percent=result.realized_margin_percent,
        exchange_rate=result.exchange_rate,
    )


async def _require_shop(session: AsyncSession, shop_binding_id: str) -> ShopBinding:
    shop = await session.get(ShopBinding, shop_binding_id)
    if shop is None:
        raise ApiProblem(404, "SHOP_NOT_FOUND", "shop binding was not found")
    return shop


@router.get("/capabilities", response_model=ToolsCapabilitiesResponse)
async def tools_capabilities(
    shop_binding_id: ShopBindingId,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    session: Annotated[AsyncSession, Depends(database_session)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> ToolsCapabilitiesResponse:
    await _require_shop(session, shop_binding_id)
    blockers = [] if runtime.translation_configured else ["BLOCKED_AZURE_TRANSLATOR_CONFIGURATION"]
    return ToolsCapabilitiesResponse(
        translation_configured=runtime.translation_configured,
        translation_provider="AZURE_TRANSLATOR_V3" if runtime.translation_configured else None,
        supported_translation_languages=["zh-Hans", "en", "ms"],
        translation_cache_enabled=False,
        blockers=blockers,
    )


@router.post("/translate", response_model=TranslationResponse)
async def translate_text(
    shop_binding_id: ShopBindingId,
    payload: TranslationInput,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=16, max_length=255),
    ],
    request: Request,
    admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> TranslationResponse:
    try:
        translation_request = TranslationRequest(
            texts=tuple(payload.texts),
            source_language=payload.source_language,
            target_language=payload.target_language,
        )
    except TranslationRequestRejected as exc:
        raise ApiProblem(422, "TRANSLATION_REQUEST_INVALID", "translation request is invalid") from exc
    if runtime.translation_provider is None:
        async with factory.begin() as session:
            await _require_shop(session, shop_binding_id)
            await record_audit_fact(
                session,
                event_type="translation.blocked",
                outcome="BLOCKED",
                actor_session_id=admin.session_id,
                shop_binding_id=shop_binding_id,
                request_id=_request_id(request),
                resource_type="translation",
                details={
                    "code": "azure_translator_not_configured",
                    "item_count": len(translation_request.texts),
                    "character_count": sum(len(text) for text in translation_request.texts),
                    "source_language": translation_request.source_language,
                    "target_language": translation_request.target_language,
                },
            )
        raise ApiProblem(
            503,
            "BLOCKED_AZURE_TRANSLATOR_CONFIGURATION",
            "Azure Translator is not configured",
        )

    intent = {
        "texts": list(translation_request.texts),
        "source_language": translation_request.source_language,
        "target_language": translation_request.target_language,
    }
    payload_hash = canonical_payload_hash(intent)
    business_key = hashlib.sha256(
        f"translation-key:{idempotency_key}".encode()
    ).hexdigest()
    async with factory.begin() as session:
        await _require_shop(session, shop_binding_id)
        operation, created = await register_operation(
            session,
            IdempotencyRequest(
                shop_binding_id=shop_binding_id,
                operation="TRANSLATE",
                business_key=business_key,
                payload_hash=payload_hash,
                idempotency_key=idempotency_key,
            ),
        )
        if not created:
            raise ApiProblem(
                409,
                "TRANSLATION_REPLAY_BLOCKED",
                "translation result is not cached; a completed or in-flight request cannot be replayed",
            )
        operation.state = WriteState.SUBMITTED.value
        operation_id = operation.id

    try:
        result = await runtime.translation_provider.translate(translation_request)
    except TranslationUpstreamError as exc:
        async with factory.begin() as session:
            operation = await session.get(IdempotentOperation, operation_id)
            if operation is not None and operation.state == WriteState.SUBMITTED.value:
                operation.state = WriteState.FAILED.value
                operation.manual_review_reason = "Azure Translator request failed"
            await record_audit_fact(
                session,
                event_type="translation.failed",
                outcome="FAILED",
                actor_session_id=admin.session_id,
                shop_binding_id=shop_binding_id,
                request_id=_request_id(request),
                resource_type="translation",
                resource_id=operation_id,
                details={
                    "code": exc.code,
                    "provider": "AZURE_TRANSLATOR_V3",
                    "item_count": len(translation_request.texts),
                },
            )
        raise ApiProblem(502, "TRANSLATION_UPSTREAM_FAILED", "translation provider is unavailable") from exc

    async with factory.begin() as session:
        operation = await session.get(IdempotentOperation, operation_id)
        if operation is None or operation.state != WriteState.SUBMITTED.value:
            raise ApiProblem(409, "TRANSLATION_STATE_CONFLICT", "translation state changed concurrently")
        operation.state = WriteState.ACTIVE.value
        operation.result_reference = result.provider
        operation.platform_request_id = result.request_id
        await record_audit_fact(
            session,
            event_type="translation.succeeded",
            outcome="SUCCESS",
            actor_session_id=admin.session_id,
            shop_binding_id=shop_binding_id,
            request_id=_request_id(request),
            resource_type="translation",
            resource_id=operation_id,
            details={
                "code": "translation_completed",
                "provider": result.provider,
                "item_count": len(result.texts),
                "character_count": sum(len(text) for text in translation_request.texts),
                "source_language": result.source_language,
                "target_language": result.target_language,
            },
        )
    return TranslationResponse(
        texts=list(result.texts),
        source_language=result.source_language,
        target_language=result.target_language,
        provider=result.provider,
        provider_request_id=result.request_id,
        cached=False,
    )


@router.post("/profit", response_model=ProfitResponse)
async def calculate_profit(
    shop_binding_id: ShopBindingId,
    payload: ProfitInput,
    request: Request,
    admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(database_session)],
) -> ProfitResponse:
    await _require_shop(session, shop_binding_id)
    try:
        inputs = _profit_inputs(payload)
        result = (
            estimate_profit(inputs)
            if payload.selling_price is None
            else profit_for_price(inputs, payload.selling_price)
        )
    except ProfitInputError as exc:
        raise ApiProblem(422, "PROFIT_REQUEST_INVALID", "profit request is invalid") from exc
    await record_audit_fact(
        session,
        event_type="profit.calculated",
        outcome="SUCCESS",
        actor_session_id=admin.session_id,
        shop_binding_id=shop_binding_id,
        request_id=_request_id(request),
        resource_type="profit",
        details={"code": "profit_calculated"},
    )
    return _profit_response(result)