"""Build short-lived commerce contexts from encrypted persisted shop facts."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EncryptedCredential, ScopeSnapshot, ShopBinding
from app.domain.enums import AuthorizationStatus, ListingMode
from app.domain.scopes import ScopeSet
from app.use_cases.commerce_context import CommerceAccessBlocked, ShopAccessContext
from shared.security import KeyRing, decrypt_text


async def load_shop_access_context(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    key_ring: KeyRing,
) -> ShopAccessContext:
    binding = await session.get(ShopBinding, shop_binding_id)
    if binding is None:
        raise CommerceAccessBlocked("shop binding was not found")
    try:
        authorization_status = AuthorizationStatus(binding.authorization_status)
        listing_mode = ListingMode(binding.listing_mode)
    except ValueError as exc:
        raise CommerceAccessBlocked("shop binding contains an unsupported state") from exc
    if authorization_status is not AuthorizationStatus.ACTIVE:
        raise CommerceAccessBlocked("shop authorization is not active")
    snapshot = await session.scalar(
        select(ScopeSnapshot)
        .where(ScopeSnapshot.shop_binding_id == binding.id)
        .order_by(ScopeSnapshot.captured_at.desc(), ScopeSnapshot.id.desc())
        .limit(1)
    )
    if snapshot is None:
        raise CommerceAccessBlocked("shop has no granted-scope snapshot")
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
    if access_credential is None or cipher_credential is None or not cipher_credential.active:
        raise CommerceAccessBlocked("active shop credentials are unavailable")
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
        authorization_status=authorization_status,
        scopes=ScopeSet.parse(snapshot.granted_scopes),
        access_token=access_token,
        shop_cipher=shop_cipher,
    )