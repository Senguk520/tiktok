"""Signed, loopback-only Collector HTTP API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import JSONResponse

from collector_app.db.base import session_scope
from collector_app.image_access import CollectorImageAccessError, read_registered_image
from collector_app.imports import confirm_import_receipt, export_result
from collector_app.jobs import create_collection_job, get_collection_job
from collector_app.sources.intents import SourceIntentError
from shared.collector_contract import (
    CollectorContractError,
    CollectorImportReceiptV1,
)
from shared.security import (
    INTERNAL_HMAC_SIGNATURE_HEADER,
    INTERNAL_HMAC_TIMESTAMP_HEADER,
    verify_internal_message,
)

_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
ResourceId = Annotated[str, Path(min_length=36, max_length=36, pattern=_UUID_PATTERN)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectorApiProblem(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.safe_message = message


class ErrorBody(_StrictModel):
    code: str
    message: str


class ErrorEnvelope(_StrictModel):
    error: ErrorBody


class CreateCollectionRequest(_StrictModel):
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


class ImportReceiptRequest(_StrictModel):
    contract: str = Field(min_length=1, max_length=128)
    version: int
    result_id: str = Field(min_length=1, max_length=128)
    draft_id: str = Field(min_length=1, max_length=128)
    envelope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created: bool


class ImportReceiptResponse(_StrictModel):
    result_id: str
    imported: bool
    newly_marked: bool


async def collector_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = getattr(request.app.state, "db_session_factory", None)
    if not isinstance(factory, async_sessionmaker):
        raise CollectorApiProblem(
            503,
            "BLOCKED_CONFIGURATION",
            "Collector persistence is unavailable",
        )
    async with session_scope(factory) as session:
        yield session


async def require_internal_hmac(request: Request) -> None:
    secret = getattr(request.app.state, "internal_hmac_secret", None)
    if not isinstance(secret, bytes) or not secret:
        raise CollectorApiProblem(
            503,
            "BLOCKED_CONFIGURATION",
            "Collector authentication is unavailable",
        )
    body = await request.body()
    if not verify_internal_message(
        secret,
        request.headers.get(INTERNAL_HMAC_SIGNATURE_HEADER, ""),
        timestamp=request.headers.get(INTERNAL_HMAC_TIMESTAMP_HEADER, ""),
        method=request.method,
        path=request.url.path,
        body=body,
    ):
        raise CollectorApiProblem(
            401,
            "INTERNAL_AUTHENTICATION_FAILED",
            "internal request authentication failed",
        )


router = APIRouter(
    prefix="/internal/v1",
    tags=["internal"],
    dependencies=[Depends(require_internal_hmac)],
)


@router.post(
    "/jobs",
    response_model=CollectionCreatedResponse,
    status_code=201,
)
async def create_job(
    payload: CreateCollectionRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(collector_session)],
) -> CollectionCreatedResponse:
    try:
        created = await create_collection_job(
            session,
            source=payload.source,
            source_mode=payload.source_mode,
            source_url=payload.source_url,
        )
    except SourceIntentError as exc:
        raise CollectorApiProblem(
            422,
            "COLLECTION_INTENT_INVALID",
            "collection intent is invalid",
        ) from exc
    if created.reused:
        response.status_code = 200
    return CollectionCreatedResponse(
        job_id=created.job_id,
        source=created.source,
        source_mode=created.source_mode,
        status=created.status,
        reused=created.reused,
    )


@router.get("/jobs/{job_id}", response_model=CollectionStatusResponse)
async def job_status(
    job_id: ResourceId,
    session: Annotated[AsyncSession, Depends(collector_session)],
) -> CollectionStatusResponse:
    try:
        snapshot = await get_collection_job(session, job_id=job_id)
    except LookupError as exc:
        raise CollectorApiProblem(
            404,
            "COLLECTOR_JOB_NOT_FOUND",
            "collection job was not found",
        ) from exc
    return CollectionStatusResponse(
        job_id=snapshot.job_id,
        source=snapshot.source,
        source_mode=snapshot.source_mode,
        status=snapshot.status,
        attempts=snapshot.attempts,
        max_attempts=snapshot.max_attempts,
        result_id=snapshot.result_id,
        imported=snapshot.imported,
        error_code=snapshot.error_code,
    )


@router.get("/results/{result_id}")
async def result_envelope(
    result_id: ResourceId,
    session: Annotated[AsyncSession, Depends(collector_session)],
) -> JSONResponse:
    try:
        envelope = await export_result(session, result_id=result_id)
    except LookupError as exc:
        raise CollectorApiProblem(
            404,
            "COLLECTOR_RESULT_NOT_FOUND",
            "collector result was not found",
        ) from exc
    except CollectorContractError as exc:
        raise CollectorApiProblem(
            409,
            "COLLECTOR_RESULT_NOT_EXPORTABLE",
            "collector result is not exportable",
        ) from exc
    return JSONResponse(content=envelope.to_mapping())


@router.get("/images/{image_record_id}")
async def image_file(
    image_record_id: ResourceId,
    session: Annotated[AsyncSession, Depends(collector_session)],
) -> Response:
    try:
        image = await read_registered_image(session, image_record_id=image_record_id)
    except LookupError as exc:
        raise CollectorApiProblem(
            404,
            "COLLECTOR_IMAGE_NOT_FOUND",
            "collector image was not found",
        ) from exc
    except CollectorImageAccessError as exc:
        code = (
            "COLLECTOR_IMAGE_UNAVAILABLE"
            if exc.code == "image_unavailable"
            else "COLLECTOR_IMAGE_INTEGRITY_FAILED"
        )
        raise CollectorApiProblem(409, code, "collector image is unavailable") from exc
    return Response(
        content=image.content,
        media_type=image.content_type,
        headers={"Content-Length": str(len(image.content))},
    )


@router.post(
    "/results/{result_id}/receipt",
    response_model=ImportReceiptResponse,
)
async def confirm_receipt(
    result_id: ResourceId,
    payload: ImportReceiptRequest,
    session: Annotated[AsyncSession, Depends(collector_session)],
) -> ImportReceiptResponse:
    if payload.result_id != result_id:
        raise CollectorApiProblem(
            409,
            "COLLECTOR_RECEIPT_REJECTED",
            "collector import receipt does not match the target result",
        )
    try:
        receipt = CollectorImportReceiptV1.from_mapping(payload.model_dump(mode="json"))
        newly_marked = await confirm_import_receipt(session, receipt=receipt)
    except LookupError as exc:
        raise CollectorApiProblem(
            404,
            "COLLECTOR_RESULT_NOT_FOUND",
            "collector result was not found",
        ) from exc
    except CollectorContractError as exc:
        raise CollectorApiProblem(
            409,
            "COLLECTOR_RECEIPT_REJECTED",
            "collector import receipt was rejected",
        ) from exc
    return ImportReceiptResponse(
        result_id=result_id,
        imported=True,
        newly_marked=newly_marked,
    )


def install_collector_api(app: FastAPI) -> None:
    app.include_router(router)

    async def handle_problem(_request: Request, exc: CollectorApiProblem) -> JSONResponse:
        body = ErrorEnvelope(error=ErrorBody(code=exc.code, message=exc.safe_message))
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))

    async def handle_validation(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        body = ErrorEnvelope(
            error=ErrorBody(
                code="REQUEST_VALIDATION_FAILED",
                message="request payload or parameters are invalid",
            )
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    async def handle_unexpected(_request: Request, _exc: Exception) -> JSONResponse:
        body = ErrorEnvelope(
            error=ErrorBody(
                code="INTERNAL_ERROR",
                message="an internal error occurred",
            )
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    app.add_exception_handler(CollectorApiProblem, handle_problem)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, handle_validation)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, handle_unexpected)


__all__ = [
    "CollectionCreatedResponse",
    "CollectionStatusResponse",
    "CreateCollectionRequest",
    "ImportReceiptRequest",
    "ImportReceiptResponse",
    "collector_session",
    "install_collector_api",
    "require_internal_hmac",
]