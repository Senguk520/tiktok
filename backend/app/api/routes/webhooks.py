"""TikTok webhook boundary; state changes remain disabled without verified signing rules."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import AuthenticatedAdmin, require_admin_session
from app.api.dependencies import session_factory
from app.api.errors import ERROR_RESPONSES, ApiProblem
from app.repositories.audit import record_audit_fact


class WebhookCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receiver_enabled: bool
    signature_contract_verified: bool
    state_changes_enabled: bool
    blockers: list[str]


router = APIRouter(
    prefix="/api/webhooks/tiktok",
    tags=["webhooks"],
    responses=ERROR_RESPONSES,
)


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and value else None


@router.get("/capabilities", response_model=WebhookCapabilitiesResponse)
async def webhook_capabilities(
    _admin: Annotated[AuthenticatedAdmin, Depends(require_admin_session)],
) -> WebhookCapabilitiesResponse:
    return WebhookCapabilitiesResponse(
        receiver_enabled=False,
        signature_contract_verified=False,
        state_changes_enabled=False,
        blockers=["BLOCKED_UNVERIFIED_TIKTOK_WEBHOOK_SIGNATURE_CONTRACT"],
    )


@router.post("")
async def receive_tiktok_webhook(
    request: Request,
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
) -> None:
    # Do not read or parse the untrusted body until TikTok's exact signature
    # input, timestamp and replay-window contract can be verified.
    async with factory.begin() as session:
        await record_audit_fact(
            session,
            event_type="webhook.rejected",
            outcome="REJECTED",
            request_id=_request_id(request),
            resource_type="webhook",
            details={
                "code": "tiktok_webhook_signature_unverified",
                "reason": "signature_contract_unverified",
            },
        )
    raise ApiProblem(
        503,
        "WEBHOOK_SIGNATURE_CONTRACT_UNVERIFIED",
        "TikTok webhook processing is disabled until the signing contract is verified",
    )