"""Protected SQLite schedule management and run history routes."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import AuthenticatedAdmin, require_admin_session, require_csrf
from app.api.dependencies import UUID_PATTERN, ShopBindingId, commerce_runtime, database_session
from app.api.errors import ERROR_RESPONSES, ApiProblem
from app.api.runtime import CommerceRuntime
from app.db.models import ScheduleJob, ScheduleRun, ShopBinding
from app.domain.enums import Scope, WriteState
from app.repositories.audit import record_audit_fact
from app.repositories.idempotency import (
    IdempotencyRequest,
    canonical_payload_hash,
    register_operation,
)
from app.use_cases.product_capabilities import evaluate_product_capabilities
from app.use_cases.scheduler import (
    ScheduleCommand,
    ScheduleJobType,
    ScheduleKind,
    ScheduleValidationError,
    change_schedule_state,
    create_schedule_job,
    list_schedule_jobs,
    list_schedule_runs,
)
from app.use_cases.shop_access import (
    ShopAccessFactsBlocked,
    require_operational_shop,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduleCapabilitiesResponse(_StrictModel):
    worker_enabled: bool
    publish_draft_enabled: bool
    order_sync_enabled: bool
    blockers: list[str]


class ScheduleCreateInput(_StrictModel):
    job_type: ScheduleJobType
    schedule_kind: ScheduleKind
    run_at: datetime
    interval_seconds: int | None = Field(default=None, ge=60, le=2_678_400)
    payload: dict[str, Any] = Field(max_length=8)


class ScheduleStateInput(_StrictModel):
    enabled: bool


class ScheduleResponse(_StrictModel):
    id: str
    job_type: str
    schedule_kind: str
    interval_seconds: int | None
    run_at: datetime | None
    next_run_at: datetime
    enabled: bool
    payload: dict[str, Any]
    required_scopes: list[str]
    required_listing_mode: str | None
    quota_cost: int
    created_at: datetime
    updated_at: datetime


class ScheduleRunResponse(_StrictModel):
    id: str
    state: str
    worker_id: str
    started_at: datetime
    finished_at: datetime | None
    summary: dict[str, Any]
    error_code: str | None


class ScheduleListResponse(_StrictModel):
    items: list[ScheduleResponse]


class ScheduleRunListResponse(_StrictModel):
    items: list[ScheduleRunResponse]


router = APIRouter(
    prefix="/api/shops/{shop_binding_id}/schedules",
    tags=["schedules"],
    responses=ERROR_RESPONSES,
)


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and value else None


async def _require_shop(session: AsyncSession, shop_binding_id: str) -> ShopBinding:
    shop = await session.get(ShopBinding, shop_binding_id)
    if shop is None:
        raise ApiProblem(404, "SHOP_NOT_FOUND", "shop binding was not found")
    return shop


def _schedule_response(job: ScheduleJob) -> ScheduleResponse:
    return ScheduleResponse.model_validate(job, from_attributes=True)


def _run_response(run: ScheduleRun) -> ScheduleRunResponse:
    return ScheduleRunResponse.model_validate(run, from_attributes=True)


@router.get("/capabilities", response_model=ScheduleCapabilitiesResponse)
async def schedule_capabilities(
    shop_binding_id: ShopBindingId,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    session: Annotated[AsyncSession, Depends(database_session)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> ScheduleCapabilitiesResponse:
    await _require_shop(session, shop_binding_id)
    product = await evaluate_product_capabilities(
        session,
        shop_binding_id=shop_binding_id,
        platform_configured=runtime.platform_configured,
        key_ring=runtime.key_ring,
        endpoint_evidence=runtime.product_capabilities,
    )
    order_blockers: list[str] = []
    if not runtime.platform_configured:
        order_blockers.append("BLOCKED_LIVE_CREDENTIALS")
    if runtime.key_ring is None:
        order_blockers.append("BLOCKED_MASTER_KEY")
    try:
        facts = await require_operational_shop(
            session,
            shop_binding_id=shop_binding_id,
        )
    except ShopAccessFactsBlocked as exc:
        order_blockers.extend(exc.blockers)
    else:
        if Scope.ORDER_INFO.value not in set(facts.snapshot.granted_scopes):
            order_blockers.append("BLOCKED_SCOPE:seller.order.info")
    blockers = tuple(
        dict.fromkeys((*product.product_submission_blockers, *order_blockers))
    )
    return ScheduleCapabilitiesResponse(
        worker_enabled=True,
        publish_draft_enabled=product.product_submission_enabled,
        order_sync_enabled=not order_blockers,
        blockers=list(blockers),
    )


@router.get("", response_model=ScheduleListResponse)
async def schedules(
    shop_binding_id: ShopBindingId,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    session: Annotated[AsyncSession, Depends(database_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> ScheduleListResponse:
    await _require_shop(session, shop_binding_id)
    return ScheduleListResponse(
        items=[
            _schedule_response(job)
            for job in await list_schedule_jobs(
                session,
                shop_binding_id=shop_binding_id,
                limit=limit,
            )
        ]
    )


@router.post("", response_model=ScheduleResponse, status_code=201)
async def create_schedule(
    shop_binding_id: ShopBindingId,
    payload: ScheduleCreateInput,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=16, max_length=255),
    ],
    request: Request,
    admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(database_session)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> ScheduleResponse:
    await _require_shop(session, shop_binding_id)
    if payload.job_type is ScheduleJobType.PUBLISH_DRAFT:
        product = await evaluate_product_capabilities(
            session,
            shop_binding_id=shop_binding_id,
            platform_configured=runtime.platform_configured,
            key_ring=runtime.key_ring,
            endpoint_evidence=runtime.product_capabilities,
        )
        if not product.product_submission_enabled:
            raise ApiProblem(
                503,
                "SCHEDULE_PUBLICATION_BLOCKED",
                "scheduled publication is unavailable until commerce preconditions are met",
            )
    if payload.job_type is ScheduleJobType.SYNC_ORDERS and not (
        runtime.platform_configured and runtime.key_ring is not None
    ):
        raise ApiProblem(
            503,
            "SCHEDULE_ORDER_SYNC_BLOCKED",
            "scheduled order synchronization is not configured",
        )
    intent = payload.model_dump(mode="json")
    intent_hash = canonical_payload_hash(intent)
    operation, created = await register_operation(
        session,
        IdempotencyRequest(
            shop_binding_id=shop_binding_id,
            operation="CREATE_SCHEDULE",
            business_key=intent_hash,
            payload_hash=intent_hash,
            idempotency_key=idempotency_key,
        ),
    )
    if not created:
        if not operation.result_reference:
            raise ApiProblem(409, "SCHEDULE_CREATE_IN_PROGRESS", "schedule creation is incomplete")
        existing = await session.get(ScheduleJob, operation.result_reference)
        if existing is None or existing.shop_binding_id != shop_binding_id:
            raise ApiProblem(409, "SCHEDULE_STATE_CONFLICT", "schedule state is inconsistent")
        return _schedule_response(existing)
    try:
        job = await create_schedule_job(
            session,
            shop_binding_id=shop_binding_id,
            command=ScheduleCommand(
                job_type=payload.job_type,
                schedule_kind=payload.schedule_kind,
                run_at=payload.run_at,
                interval_seconds=payload.interval_seconds,
                payload=payload.payload,
            ),
        )
    except ScheduleValidationError as exc:
        raise ApiProblem(422, "SCHEDULE_REQUEST_INVALID", "schedule request is invalid") from exc
    operation.state = WriteState.ACTIVE.value
    operation.result_reference = job.id
    await record_audit_fact(
        session,
        event_type="schedule.created",
        outcome="SUCCESS",
        actor_session_id=admin.session_id,
        shop_binding_id=shop_binding_id,
        request_id=_request_id(request),
        resource_type="schedule_job",
        resource_id=job.id,
        details={
            "code": "schedule_created",
            "job_type": job.job_type,
            "schedule_kind": job.schedule_kind,
        },
    )
    return _schedule_response(job)


@router.patch(
    "/{schedule_job_id}",
    response_model=ScheduleResponse,
)
async def set_schedule_state(
    shop_binding_id: ShopBindingId,
    schedule_job_id: Annotated[
        str,
        Path(min_length=36, max_length=36, pattern=UUID_PATTERN),
    ],
    payload: ScheduleStateInput,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=16, max_length=255),
    ],
    request: Request,
    admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    session: Annotated[AsyncSession, Depends(database_session)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> ScheduleResponse:
    job = await session.get(ScheduleJob, schedule_job_id)
    if job is None or job.shop_binding_id != shop_binding_id:
        raise ApiProblem(404, "SCHEDULE_NOT_FOUND", "schedule was not found")
    if payload.enabled and job.job_type == ScheduleJobType.PUBLISH_DRAFT.value:
        product = await evaluate_product_capabilities(
            session,
            shop_binding_id=shop_binding_id,
            platform_configured=runtime.platform_configured,
            key_ring=runtime.key_ring,
            endpoint_evidence=runtime.product_capabilities,
        )
        if not product.product_submission_enabled:
            raise ApiProblem(
                503,
                "SCHEDULE_PUBLICATION_BLOCKED",
                "scheduled publication is unavailable",
            )
    if payload.enabled and job.job_type == ScheduleJobType.SYNC_ORDERS.value and not (
        runtime.platform_configured and runtime.key_ring is not None
    ):
        raise ApiProblem(
            503,
            "SCHEDULE_ORDER_SYNC_BLOCKED",
            "scheduled order synchronization is not configured",
        )
    state_hash = canonical_payload_hash(
        {"schedule_job_id": schedule_job_id, "enabled": payload.enabled}
    )
    business_key = hashlib.sha256(
        f"schedule-state-key:{idempotency_key}".encode()
    ).hexdigest()
    operation, created = await register_operation(
        session,
        IdempotencyRequest(
            shop_binding_id=shop_binding_id,
            operation="SET_SCHEDULE_STATE",
            business_key=business_key,
            payload_hash=state_hash,
            idempotency_key=idempotency_key,
        ),
    )
    if not created:
        if job.enabled != payload.enabled:
            raise ApiProblem(
                409,
                "SCHEDULE_REPLAY_STALE",
                "the persisted schedule state no longer matches this completed request",
            )
        return _schedule_response(job)
    try:
        changed = await change_schedule_state(
            session,
            shop_binding_id=shop_binding_id,
            schedule_job_id=schedule_job_id,
            enabled=payload.enabled,
        )
    except ScheduleValidationError as exc:
        raise ApiProblem(
            422,
            "SCHEDULE_REQUEST_INVALID",
            "schedule request is invalid",
        ) from exc
    if not changed:
        raise ApiProblem(409, "SCHEDULE_STATE_CONFLICT", "schedule state changed concurrently")
    operation.state = WriteState.ACTIVE.value
    operation.result_reference = schedule_job_id
    await record_audit_fact(
        session,
        event_type="schedule.enabled" if payload.enabled else "schedule.disabled",
        outcome="SUCCESS",
        actor_session_id=admin.session_id,
        shop_binding_id=shop_binding_id,
        request_id=_request_id(request),
        resource_type="schedule_job",
        resource_id=schedule_job_id,
        details={
            "code": "schedule_enabled" if payload.enabled else "schedule_disabled",
            "job_type": job.job_type,
        },
    )
    await session.refresh(job)
    return _schedule_response(job)


@router.get(
    "/{schedule_job_id}/runs",
    response_model=ScheduleRunListResponse,
)
async def schedule_runs(
    shop_binding_id: ShopBindingId,
    schedule_job_id: Annotated[
        str,
        Path(min_length=36, max_length=36, pattern=UUID_PATTERN),
    ],
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    session: Annotated[AsyncSession, Depends(database_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> ScheduleRunListResponse:
    job = await session.get(ScheduleJob, schedule_job_id)
    if job is None or job.shop_binding_id != shop_binding_id:
        raise ApiProblem(404, "SCHEDULE_NOT_FOUND", "schedule was not found")
    return ScheduleRunListResponse(
        items=[
            _run_response(run)
            for run in await list_schedule_runs(
                session,
                shop_binding_id=shop_binding_id,
                schedule_job_id=schedule_job_id,
                limit=limit,
            )
        ]
    )