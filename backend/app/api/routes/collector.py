"""Browser-facing collection intents and Core-owned import orchestration."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import AuthenticatedAdmin, require_admin_session, require_csrf
from app.api.dependencies import ShopBindingId, session_factory
from app.api.errors import ERROR_RESPONSES, ApiProblem
from app.integrations.collector import (
    CollectorClientError,
    CollectorHttpClient,
    CollectorJobCreated,
    CollectorJobStatus,
)
from app.use_cases.collector_imports import (
    CollectorImportConflict,
    CoreCollectorImportService,
)
from shared.collector_contract import CollectorContractError

_RESOURCE_ID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
ResourceId = Annotated[
    str,
    Path(min_length=36, max_length=36, pattern=_RESOURCE_ID_PATTERN),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectionIntentRequest(_StrictModel):
    source: str = Field(min_length=1, max_length=32)
    source_mode: str = Field(min_length=1, max_length=32)
    source_url: str = Field(min_length=1, max_length=2048)


class CollectionCreatedResponse(_StrictModel):
    job_id: str
    source: str
    source_mode: str
    status: str
    reused: bool


class CollectionStatusResponse(_StrictModel):
    job_id: str
    source: str
    source_mode: str
    status: str
    attempts: int
    max_attempts: int
    result_id: str | None
    imported: bool
    error_code: str | None


class CollectorImportResponse(_StrictModel):
    result_id: str
    draft_id: str
    created: bool
    collector_acknowledged: bool
    collector_acknowledgement_replayed: bool


def collector_http_client(request: Request) -> CollectorHttpClient:
    client = getattr(request.app.state, "collector_http_client", None)
    if not isinstance(client, CollectorHttpClient):
        raise ApiProblem(
            503,
            "COLLECTOR_CONFIGURATION_BLOCKED",
            "Collector integration is not configured",
        )
    return client


def _raise_client_problem(exc: CollectorClientError) -> None:
    if exc.status_code == 404:
        raise ApiProblem(404, "COLLECTOR_RESOURCE_NOT_FOUND", "Collector resource was not found") from exc
    if exc.status_code == 409:
        raise ApiProblem(409, "COLLECTOR_STATE_CONFLICT", "Collector state does not allow the operation") from exc
    if exc.status_code == 422:
        raise ApiProblem(422, "COLLECTION_INTENT_INVALID", "collection intent is invalid") from exc
    if exc.code == "COLLECTOR_RESOURCE_ID_INVALID":
        raise ApiProblem(422, "COLLECTOR_RESOURCE_ID_INVALID", "Collector resource id is invalid") from exc
    if exc.code in {"COLLECTOR_RESPONSE_INVALID", "COLLECTOR_RESPONSE_TOO_LARGE"}:
        raise ApiProblem(502, "COLLECTOR_RESPONSE_INVALID", "Collector returned an invalid response") from exc
    raise ApiProblem(502, "COLLECTOR_UNAVAILABLE", "Collector service is unavailable") from exc


def _created_response(value: CollectorJobCreated) -> CollectionCreatedResponse:
    return CollectionCreatedResponse(
        job_id=value.job_id,
        source=value.source,
        source_mode=value.source_mode,
        status=value.status,
        reused=value.reused,
    )


def _status_response(value: CollectorJobStatus) -> CollectionStatusResponse:
    return CollectionStatusResponse(
        job_id=value.job_id,
        source=value.source,
        source_mode=value.source_mode,
        status=value.status,
        attempts=value.attempts,
        max_attempts=value.max_attempts,
        result_id=value.result_id,
        imported=value.imported,
        error_code=value.error_code,
    )


router = APIRouter(
    prefix="/api/collector",
    tags=["collector"],
    responses=ERROR_RESPONSES,
)


@router.post("/jobs", response_model=CollectionCreatedResponse, status_code=201)
async def create_collection(
    payload: CollectionIntentRequest,
    response: Response,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    client: Annotated[CollectorHttpClient, Depends(collector_http_client)],
) -> CollectionCreatedResponse:
    try:
        created = await client.create_job(
            source=payload.source,
            source_mode=payload.source_mode,
            source_url=payload.source_url,
        )
    except CollectorClientError as exc:
        _raise_client_problem(exc)
    if created.reused:
        response.status_code = 200
    return _created_response(created)


@router.get("/jobs/{job_id}", response_model=CollectionStatusResponse)
async def collection_status(
    job_id: ResourceId,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    client: Annotated[CollectorHttpClient, Depends(collector_http_client)],
) -> CollectionStatusResponse:
    try:
        return _status_response(await client.get_job(job_id))
    except CollectorClientError as exc:
        _raise_client_problem(exc)


@router.post(
    "/shops/{shop_binding_id}/results/{result_id}/import",
    response_model=CollectorImportResponse,
    status_code=201,
)
async def import_collection_result(
    shop_binding_id: ShopBindingId,
    result_id: ResourceId,
    response: Response,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_csrf)],
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
    client: Annotated[CollectorHttpClient, Depends(collector_http_client)],
) -> CollectorImportResponse:
    try:
        envelope = await client.export_result(result_id)
    except CollectorClientError as exc:
        _raise_client_problem(exc)
    try:
        receipt = await CoreCollectorImportService(factory).import_product(
            shop_binding_id=shop_binding_id,
            envelope=envelope,
        )
    except CollectorImportConflict as exc:
        raise ApiProblem(
            409,
            "COLLECTOR_IMPORT_CONFLICT",
            "Collector result conflicts with an existing import",
        ) from exc
    except LookupError as exc:
        raise ApiProblem(404, "SHOP_NOT_FOUND", "target shop binding was not found") from exc
    except CollectorContractError as exc:
        raise ApiProblem(
            502,
            "COLLECTOR_RESPONSE_INVALID",
            "Collector returned an invalid import envelope",
        ) from exc
    try:
        acknowledgement = await client.acknowledge_receipt(receipt)
    except CollectorClientError as exc:
        _raise_client_problem(exc)
    if not receipt.created:
        response.status_code = 200
    return CollectorImportResponse(
        result_id=receipt.result_id,
        draft_id=receipt.draft_id,
        created=receipt.created,
        collector_acknowledged=True,
        collector_acknowledgement_replayed=not acknowledgement.newly_marked,
    )


__all__ = [
    "CollectionCreatedResponse",
    "CollectionIntentRequest",
    "CollectionStatusResponse",
    "CollectorImportResponse",
    "collector_http_client",
    "router",
]