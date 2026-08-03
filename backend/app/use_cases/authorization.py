"""Single-shop OAuth transaction, encrypted binding, refresh, and deauthorization."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    EncryptedCredential,
    OAuthTransaction,
    ScopeSnapshot,
    ShopBinding,
)
from app.domain.enums import AuthorizationStatus, Scope
from app.integrations.tiktok.client import TikTokClient
from app.integrations.tiktok.oauth import OAuthClient, OAuthConfig, TokenSet
from app.repositories.jobs import disable_shop_jobs
from shared.security import KeyRing, MasterKey, decrypt_text, encrypt_value


class AuthorizationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthorizationStart:
    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthorizedShop:
    shop_id: str
    cipher: str
    region: str
    shop_code: str | None = None
    seller_type: str | None = None
    shop_status: str = "UNKNOWN"
    kyc_status: str = "UNKNOWN"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AuthorizedShop:
        try:
            shop_id = str(value.get("id") or value["shop_id"])
            cipher = str(value.get("cipher") or value["shop_cipher"])
            region = str(value.get("region") or value["region_code"])
        except (KeyError, TypeError) as exc:
            raise AuthorizationError("authorized shop response lacks identity fields") from exc
        if not shop_id or not cipher or not region:
            raise AuthorizationError("authorized shop identity fields cannot be empty")
        return cls(
            shop_id=shop_id,
            cipher=cipher,
            region=region,
            shop_code=str(value["code"]) if value.get("code") else None,
            seller_type=str(value["seller_type"]) if value.get("seller_type") else None,
            shop_status=str(value.get("shop_status", "UNKNOWN")),
            kyc_status=str(value.get("kyc_status", "UNKNOWN")),
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def begin_authorization(
    session: AsyncSession,
    config: OAuthConfig,
    *,
    expected_account: str | None = None,
    ttl: timedelta = timedelta(minutes=10),
    now: datetime | None = None,
) -> AuthorizationStart:
    if ttl <= timedelta(0) or ttl > timedelta(minutes=30):
        raise ValueError("OAuth state TTL must be within 30 minutes")
    current = datetime.now(UTC) if now is None else now
    raw_state = secrets.token_urlsafe(32)
    transaction = OAuthTransaction(
        state_digest=_digest(raw_state),
        expected_account=expected_account,
        status="PENDING",
        expires_at=current + ttl,
    )
    session.add(transaction)
    await session.flush()
    return AuthorizationStart(config.authorization_url(raw_state), transaction.expires_at)


async def consume_authorization_state(
    session: AsyncSession,
    raw_state: str,
    *,
    now: datetime | None = None,
) -> OAuthTransaction:
    if not raw_state:
        raise AuthorizationError("OAuth state is required")
    current = datetime.now(UTC) if now is None else now
    digest = _digest(raw_state)
    transaction = await session.scalar(
        select(OAuthTransaction).where(OAuthTransaction.state_digest == digest)
    )
    if (
        transaction is None
        or transaction.status != "PENDING"
        or transaction.consumed_at is not None
        or transaction.expires_at.replace(tzinfo=UTC) <= current
    ):
        raise AuthorizationError("OAuth state is invalid, expired, or already used")
    result = await session.execute(
        update(OAuthTransaction)
        .where(
            OAuthTransaction.id == transaction.id,
            OAuthTransaction.status == "PENDING",
            OAuthTransaction.consumed_at.is_(None),
        )
        .values(status="CONSUMED", consumed_at=current)
    )
    if result.rowcount != 1:
        raise AuthorizationError("OAuth state was consumed concurrently")
    transaction.status = "CONSUMED"
    transaction.consumed_at = current
    return transaction


async def get_authorized_shops(
    client: TikTokClient,
    *,
    access_token: str,
) -> tuple[AuthorizedShop, ...]:
    result = await client.request("authorization.shops", access_token=access_token)
    data = result.data
    rows = data.get("shops") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise AuthorizationError("authorized shop response has no shop list")
    return tuple(AuthorizedShop.from_mapping(item) for item in rows if isinstance(item, dict))


def _select_single_shop(
    shops: Sequence[AuthorizedShop],
    selected_shop_id: str | None,
) -> AuthorizedShop:
    if not shops:
        raise AuthorizationError("authorization returned no shops")
    if selected_shop_id is None:
        if len(shops) != 1:
            raise AuthorizationError("explicit shop selection is required for multiple shops")
        return shops[0]
    matches = [shop for shop in shops if shop.shop_id == selected_shop_id]
    if len(matches) != 1:
        raise AuthorizationError("selected shop is not in the authorized shop set")
    return matches[0]


def _encrypted_credential(
    *,
    owner_kind: str,
    owner_id: str,
    credential_kind: str,
    plaintext: str,
    key: MasterKey,
) -> EncryptedCredential:
    aad = f"tiktok:{owner_kind}:{owner_id}:{credential_kind}"
    encrypted = encrypt_value(plaintext, key, aad=aad)
    return EncryptedCredential(
        owner_kind=owner_kind,
        owner_id=owner_id,
        credential_kind=credential_kind,
        ciphertext=encrypted.encode(),
        key_version=key.version,
        aad_context=aad,
    )


async def bind_authorization(
    session: AsyncSession,
    *,
    tokens: TokenSet,
    shops: Sequence[AuthorizedShop],
    key: MasterKey,
    expected_scopes: Iterable[Scope],
    selected_shop_id: str | None = None,
) -> ShopBinding:
    selected = _select_single_shop(shops, selected_shop_id)
    existing = await session.scalar(
        select(ShopBinding).where(ShopBinding.shop_id == selected.shop_id)
    )
    if existing is not None and existing.open_id != tokens.open_id:
        raise AuthorizationError("shop is already bound to another authorization subject")
    access = _encrypted_credential(
        owner_kind="authorization",
        owner_id=tokens.open_id,
        credential_kind="access_token",
        plaintext=tokens.access_token,
        key=key,
    )
    refresh = _encrypted_credential(
        owner_kind="authorization",
        owner_id=tokens.open_id,
        credential_kind="refresh_token",
        plaintext=tokens.refresh_token,
        key=key,
    )
    cipher = _encrypted_credential(
        owner_kind="shop",
        owner_id=selected.shop_id,
        credential_kind="shop_cipher",
        plaintext=selected.cipher,
        key=key,
    )
    session.add_all((access, refresh, cipher))
    await session.flush()
    binding = existing or ShopBinding(
        open_id=tokens.open_id,
        shop_id=selected.shop_id,
        region=selected.region,
    )
    binding.shop_code = selected.shop_code
    binding.seller_type = selected.seller_type
    binding.shop_status = selected.shop_status
    binding.kyc_status = selected.kyc_status
    binding.shop_cipher_credential_id = cipher.id
    binding.authorization_status = AuthorizationStatus.ACTIVE.value
    binding.deauthorized_at = None
    if existing is None:
        session.add(binding)
        await session.flush()
    gap = tokens.granted_scopes.gap(expected_scopes)
    session.add(
        ScopeSnapshot(
            shop_binding_id=binding.id,
            granted_scopes=sorted(item.value for item in tokens.granted_scopes.values),
            missing_scopes=sorted(item.value for item in gap.missing),
            access_expires_at=tokens.access_expires_at,
        )
    )
    await session.flush()
    return binding


async def refresh_if_due(
    session: AsyncSession,
    *,
    binding: ShopBinding,
    oauth: OAuthClient,
    key_ring: KeyRing,
    current_key: MasterKey,
    expected_scopes: Iterable[Scope],
    now: datetime | None = None,
    margin: timedelta = timedelta(hours=12),
) -> bool:
    current = datetime.now(UTC) if now is None else now
    snapshot = await session.scalar(
        select(ScopeSnapshot)
        .where(ScopeSnapshot.shop_binding_id == binding.id)
        .order_by(ScopeSnapshot.captured_at.desc())
        .limit(1)
    )
    if snapshot is None or snapshot.access_expires_at is None:
        raise AuthorizationError("access-token expiry is unknown")
    expiry = snapshot.access_expires_at.replace(tzinfo=UTC)
    if expiry > current + margin:
        return False
    refresh_credential = await session.scalar(
        select(EncryptedCredential).where(
            EncryptedCredential.owner_kind == "authorization",
            EncryptedCredential.owner_id == binding.open_id,
            EncryptedCredential.credential_kind == "refresh_token",
            EncryptedCredential.active.is_(True),
        )
    )
    if refresh_credential is None:
        raise AuthorizationError("active refresh credential is unavailable")
    refresh_token = decrypt_text(
        refresh_credential.ciphertext,
        key_ring,
        aad=refresh_credential.aad_context,
    )
    tokens = await oauth.refresh(refresh_token)
    if tokens.open_id != binding.open_id:
        raise AuthorizationError("refreshed token subject changed")
    await session.execute(
        update(EncryptedCredential)
        .where(
            EncryptedCredential.owner_kind == "authorization",
            EncryptedCredential.owner_id == binding.open_id,
            EncryptedCredential.active.is_(True),
        )
        .values(active=False)
    )
    session.add_all(
        (
            _encrypted_credential(
                owner_kind="authorization",
                owner_id=binding.open_id,
                credential_kind="access_token",
                plaintext=tokens.access_token,
                key=current_key,
            ),
            _encrypted_credential(
                owner_kind="authorization",
                owner_id=binding.open_id,
                credential_kind="refresh_token",
                plaintext=tokens.refresh_token,
                key=current_key,
            ),
        )
    )
    gap = tokens.granted_scopes.gap(expected_scopes)
    session.add(
        ScopeSnapshot(
            shop_binding_id=binding.id,
            granted_scopes=sorted(item.value for item in tokens.granted_scopes.values),
            missing_scopes=sorted(item.value for item in gap.missing),
            access_expires_at=tokens.access_expires_at,
        )
    )
    return True


async def deauthorize_shop(
    session: AsyncSession,
    shop_binding_id: str,
    *,
    now: datetime | None = None,
) -> int:
    current = datetime.now(UTC) if now is None else now
    binding = await session.get(ShopBinding, shop_binding_id)
    if binding is None:
        return 0
    binding.authorization_status = AuthorizationStatus.DEAUTHORIZED.value
    binding.deauthorized_at = current
    binding.listing_mode = "UNKNOWN"
    await session.execute(
        update(EncryptedCredential)
        .where(
            (
                (EncryptedCredential.owner_kind == "authorization")
                & (EncryptedCredential.owner_id == binding.open_id)
            )
            | (
                (EncryptedCredential.owner_kind == "shop")
                & (EncryptedCredential.owner_id == binding.shop_id)
            )
        )
        .values(active=False)
    )
    return await disable_shop_jobs(session, binding.id)