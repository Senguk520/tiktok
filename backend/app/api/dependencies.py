"""Short-lived database, runtime, and shop-context dependencies for API routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import ApiProblem
from app.api.runtime import CommerceRuntime
from app.db.base import session_scope
from app.use_cases.commerce_context import ShopAccessContext
from app.use_cases.shop_access import load_shop_access_context

UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
ShopBindingId = Annotated[
    str,
    Path(min_length=36, max_length=36, pattern=UUID_PATTERN),
]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=16,
        max_length=255,
        pattern=r"^[!-~]{16,255}$",
    ),
]


def commerce_runtime(request: Request) -> CommerceRuntime:
    runtime = getattr(request.app.state, "commerce_runtime", None)
    if not isinstance(runtime, CommerceRuntime):
        raise ApiProblem(503, "BLOCKED_CONFIGURATION", "commerce runtime is unavailable")
    return runtime


async def database_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = getattr(request.app.state, "db_session_factory", None)
    if not isinstance(factory, async_sessionmaker):
        raise ApiProblem(503, "BLOCKED_CONFIGURATION", "database session factory is unavailable")
    async with session_scope(factory) as session:
        yield session


def session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "db_session_factory", None)
    if not isinstance(factory, async_sessionmaker):
        raise ApiProblem(503, "BLOCKED_CONFIGURATION", "database session factory is unavailable")
    return factory


async def shop_access_context(
    shop_binding_id: ShopBindingId,
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
    runtime: Annotated[CommerceRuntime, Depends(commerce_runtime)],
) -> ShopAccessContext:
    if runtime.key_ring is None:
        raise ApiProblem(503, "BLOCKED_CONFIGURATION", "commerce encryption key is not configured")
    async with factory() as session:
        return await load_shop_access_context(
            session,
            shop_binding_id=shop_binding_id,
            key_ring=runtime.key_ring,
        )