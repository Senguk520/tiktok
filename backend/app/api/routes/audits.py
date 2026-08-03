"""Read-only access to allowlisted, redacted Core audit facts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import AuthenticatedAdmin, require_admin_session
from app.api.dependencies import ShopBindingId, database_session
from app.api.errors import ERROR_RESPONSES, ApiProblem
from app.db.models import AuditLog, ShopBinding
from app.repositories.audit import list_audit_facts


class AuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    event_type: str
    request_id: str | None
    resource_type: str | None
    resource_id: str | None
    outcome: str
    details: dict[str, Any]
    created_at: datetime


class AuditListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[AuditResponse]


router = APIRouter(
    prefix="/api/shops/{shop_binding_id}/audits",
    tags=["audits"],
    responses=ERROR_RESPONSES,
)


def _response(record: AuditLog) -> AuditResponse:
    return AuditResponse(
        id=record.id,
        event_type=record.event_type,
        request_id=record.request_id,
        resource_type=record.resource_type,
        resource_id=record.resource_id,
        outcome=record.outcome,
        details=dict(record.redacted_details),
        created_at=record.created_at,
    )


@router.get("", response_model=AuditListResponse)
async def audits(
    shop_binding_id: ShopBindingId,
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
    session: Annotated[AsyncSession, Depends(database_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    before: datetime | None = None,
) -> AuditListResponse:
    if await session.get(ShopBinding, shop_binding_id) is None:
        raise ApiProblem(404, "SHOP_NOT_FOUND", "shop binding was not found")
    return AuditListResponse(
        items=[
            _response(record)
            for record in await list_audit_facts(
                session,
                shop_binding_id=shop_binding_id,
                limit=limit,
                before=before,
            )
        ]
    )