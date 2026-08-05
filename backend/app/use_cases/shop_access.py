"""Build short-lived commerce contexts from encrypted persisted shop facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EncryptedCredential, ScopeSnapshot, ShopBinding
from app.domain.enums import AuthorizationStatus, ListingMode
from app.domain.scopes import ScopeSet
from app.use_cases.commerce_context import (
    CommerceAccessBlocked,
    ShopAccessContext,
)
from shared.security import KeyRing, decrypt_text

_BLOCKER_MESSAGES = {
    "BLOCKED_SHOP_AUTHORIZATION": "shop authorization is not active",
    "BLOCKED_SHOP_STATUS": "shop status is not active",
    "BLOCKED_KYC_STATUS": "shop KYC status is not verified",
    "BLOCKED_SCOPE_SNAPSHOT": "shop has no granted-scope snapshot",
    "BLOCKED_TOKEN_EXPIRY_UNKNOWN": "access-token expiry is unknown",
    "BLOCKED_ACCESS_TOKEN_EXPIRED": "access token has expired",
    "BLOCKED_ACCESS_CREDENTIAL": "active access-token credential is unavailable",
    "BLOCKED_SHOP_CIPHER": "active shop-cipher credential is unavailable",
}


class ShopAccessFactsBlocked(PermissionError):
    """Raised when persisted shop facts cannot authorize commerce access."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        self.blockers = blockers
        message = _BLOCKER_MESSAGES.get(
            blockers[0] if blockers else "",
            "shop access facts are blocked",
        )
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class OperationalShopFacts:
    binding: ShopBinding
    snapshot: ScopeSnapshot
    access_credential: EncryptedCredential
    cipher_credential: EncryptedCredential


def shop_state_blockers(binding: ShopBinding) -> tuple[str, ...]:
    blockers: list[str] = []
    if binding.authorization_status != AuthorizationStatus.ACTIVE.value:
        blockers.append("BLOCKED_SHOP_AUTHORIZATION")
    if binding.shop_status != "ACTIVE":
        blockers.append("BLOCKED_SHOP_STATUS")
    if binding.kyc_status != "VERIFIED":
        blockers.append("BLOCKED_KYC_STATUS")
    return tuple(blockers)


def shop_token_blockers(
    snapshot: ScopeSnapshot | None,
    *,
    now: datetime,
) -> tuple[str, ...]:
    if snapshot is None:
        return ("BLOCKED_SCOPE_SNAPSHOT",)
    if snapshot.access_expires_at is None:
        return ("BLOCKED_TOKEN_EXPIRY_UNKNOWN",)
    if _utc(snapshot.access_expires_at) <= _utc(now):
        return ("BLOCKED_ACCESS_TOKEN_EXPIRED",)
    return ()


def shop_credential_blockers(
    access_credential: EncryptedCredential | None,
    cipher_credential: EncryptedCredential | None,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if access_credential is None or not access_credential.active:
        blockers.append("BLOCKED_ACCESS_CREDENTIAL")
    if cipher_credential is None or not cipher_credential.active:
        blockers.append("BLOCKED_SHOP_CIPHER")
    return tuple(blockers)


async def require_operational_shop(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    now: datetime | None = None,
) -> OperationalShopFacts:
    """Require active authorization, shop/KYC facts, and live credentials."""

    current = datetime.now(UTC) if now is None else _utc(now)
    binding = await session.get(ShopBinding, shop_binding_id)
    if binding is None:
        raise ShopAccessFactsBlocked(("BLOCKED_SHOP_BINDING",))
    snapshot = await session.scalar(
        select(ScopeSnapshot)
        .where(ScopeSnapshot.shop_binding_id == binding.id)
        .order_by(ScopeSnapshot.captured_at.desc(), ScopeSnapshot.id.desc())
        .limit(1)
    )
    access_credential = await session.scalar(
        select(EncryptedCredential)
        .where(
            EncryptedCredential.owner_kind == "authorization",
            EncryptedCredential.owner_id == binding.open_id,
            EncryptedCredential.credential_kind == "access_token",
            EncryptedCredential.active.is_(True),
        )
        .order_by(EncryptedCredential.updated_at.desc(), EncryptedCredential.id.desc())
        .limit(1)
    )
    cipher_credential = (
        await session.get(EncryptedCredential, binding.shop_cipher_credential_id)
        if binding.shop_cipher_credential_id
        else None
    )
    blockers = (
        *shop_state_blockers(binding),
        *shop_token_blockers(snapshot, now=current),
        *shop_credential_blockers(access_credential, cipher_credential),
    )
    if blockers:
        raise ShopAccessFactsBlocked(tuple(blockers))
    if snapshot is None or access_credential is None or cipher_credential is None:
        raise ShopAccessFactsBlocked(("BLOCKED_SHOP_ACCESS",))
    return OperationalShopFacts(
        binding=binding,
        snapshot=snapshot,
        access_credential=access_credential,
        cipher_credential=cipher_credential,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def load_shop_access_context(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    key_ring: KeyRing,
    now: datetime | None = None,
) -> ShopAccessContext:
    try:
        facts = await require_operational_shop(
            session,
            shop_binding_id=shop_binding_id,
            now=now,
        )
        binding = facts.binding
        snapshot = facts.snapshot
        access_credential = facts.access_credential
        cipher_credential = facts.cipher_credential
        listing_mode = ListingMode(binding.listing_mode)
    except (ShopAccessFactsBlocked, ValueError) as exc:
        raise CommerceAccessBlocked(str(exc)) from exc
    access_token = decrypt_text(
        access_credential.ciphertext,
        key_ring,
        aad=access_credential.aad_context,
    )
    shop_cipher = decrypt_text(
        cipher_credential.ciphertext,
        key_ring,
        aad=cipher_credential.aad_context,
    )
    return ShopAccessContext(
        shop_binding_id=binding.id,
        shop_id=binding.shop_id,
        region=binding.region,
        listing_mode=listing_mode,
        authorization_status=AuthorizationStatus.ACTIVE,
        scopes=ScopeSet.parse(snapshot.granted_scopes),
        access_token=access_token,
        shop_cipher=shop_cipher,
    )