"""Per-shop product capability decisions from persisted facts and endpoint evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EncryptedCredential, ScopeSnapshot, ShopBinding
from app.domain.enums import ListingMode
from app.domain.scopes import ScopeSet
from app.integrations.tiktok.endpoints import ENDPOINTS, Endpoint
from app.use_cases.commerce_context import CommerceAccessBlocked
from app.use_cases.listing_mode import ListingModeBlocked, assess_persisted_listing_mode
from app.use_cases.products import ProductCapabilityEvidence
from app.use_cases.shop_access import (
    load_shop_access_context,
    shop_credential_blockers,
    shop_state_blockers,
    shop_token_blockers,
)
from shared.security import KeyRing


@dataclass(frozen=True, slots=True)
class ProductCapabilityDecision:
    platform_configured: bool
    master_key_configured: bool
    listing_mode: ListingMode
    image_upload_enabled: bool
    product_submission_enabled: bool
    image_upload_blockers: tuple[str, ...]
    product_submission_blockers: tuple[str, ...]

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.image_upload_blockers, *self.product_submission_blockers)))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _product_create_chain_endpoint_keys(mode: ListingMode) -> tuple[str, ...]:
    chains = {
        ListingMode.LOCAL_REPLICATION: (
            "product.image.upload",
            "local.create",
            "local.search",
            "local.get",
        ),
        ListingMode.GLOBAL_LEGACY: (
            "product.image.upload",
            "global.create",
            "global.search",
            "global.get",
        ),
    }
    return chains.get(mode, ())


def _capability_registry(
    capabilities: ProductCapabilityEvidence,
) -> Mapping[str, Endpoint] | None:
    registry = getattr(capabilities, "registry", None)
    return registry if isinstance(registry, Mapping) else None


def _endpoint_evidence_blockers(
    capabilities: ProductCapabilityEvidence,
    endpoint_keys: tuple[str, ...],
    *,
    legacy_flag: str,
    legacy_blocker: str,
) -> tuple[str, ...]:
    registry = _capability_registry(capabilities)
    if registry is None:
        return () if bool(getattr(capabilities, legacy_flag, False)) else (legacy_blocker,)
    blockers: list[str] = []
    for endpoint_key in endpoint_keys:
        selected = registry.get(endpoint_key)
        if selected is None:
            blockers.append(f"BLOCKED_ENDPOINT_NOT_REGISTERED:{endpoint_key}")
            continue
        if not getattr(selected, "verified", True):
            blockers.append(f"BLOCKED_ENDPOINT_UNVERIFIED:{endpoint_key}")
        if not selected.enabled:
            blockers.append(f"BLOCKED_ENDPOINT_DISABLED:{endpoint_key}")
    return tuple(blockers)


def _scope_blockers(
    scopes: ScopeSet,
    capabilities: ProductCapabilityEvidence,
    endpoint_keys: tuple[str, ...],
) -> tuple[str, ...]:
    registry = _capability_registry(capabilities) or ENDPOINTS
    required = {
        selected.scope
        for key in endpoint_keys
        if (selected := registry.get(key)) is not None
    }
    return tuple(
        f"BLOCKED_SCOPE:{scope.value}"
        for scope in sorted(scopes.gap(required).missing, key=lambda item: item.value)
    )


async def evaluate_product_capabilities(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    platform_configured: bool,
    key_ring: KeyRing | None,
    endpoint_evidence: ProductCapabilityEvidence,
    now: datetime | None = None,
) -> ProductCapabilityDecision:
    current = datetime.now(UTC) if now is None else _utc(now)
    common: list[str] = []
    if not platform_configured:
        common.append("BLOCKED_LIVE_CREDENTIALS")
    if key_ring is None:
        common.append("BLOCKED_MASTER_KEY")

    binding = await session.get(ShopBinding, shop_binding_id)
    if binding is None:
        common.append("BLOCKED_SHOP_BINDING")
        return ProductCapabilityDecision(
            platform_configured=platform_configured,
            master_key_configured=key_ring is not None,
            listing_mode=ListingMode.UNKNOWN,
            image_upload_enabled=False,
            product_submission_enabled=False,
            image_upload_blockers=tuple(common),
            product_submission_blockers=tuple(common),
        )

    snapshot = await session.scalar(
        select(ScopeSnapshot)
        .where(ScopeSnapshot.shop_binding_id == binding.id)
        .order_by(ScopeSnapshot.captured_at.desc(), ScopeSnapshot.id.desc())
        .limit(1)
    )
    scopes = (
        ScopeSet(frozenset())
        if snapshot is None
        else ScopeSet.parse(snapshot.granted_scopes)
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
    common.extend(shop_state_blockers(binding))
    common.extend(shop_token_blockers(snapshot, now=current))
    common.extend(shop_credential_blockers(access_credential, cipher_credential))

    try:
        listing_decision = await assess_persisted_listing_mode(
            session,
            shop_binding_id=binding.id,
        )
    except ListingModeBlocked:
        listing_mode = ListingMode.UNKNOWN
        common.append("BLOCKED_LISTING_MODE_UNKNOWN")
    else:
        listing_mode = listing_decision.mode
        if not listing_decision.writable:
            common.append(
                "BLOCKED_LISTING_MODE_CONFLICT"
                if "conflicting listing-mode evidence" in listing_decision.blockers
                else "BLOCKED_LISTING_MODE_UNKNOWN"
            )
        elif binding.listing_mode != listing_mode.value:
            common.append("BLOCKED_LISTING_MODE_STATE_MISMATCH")

    if (
        key_ring is not None
        and not shop_state_blockers(binding)
        and not shop_token_blockers(snapshot, now=current)
        and not shop_credential_blockers(access_credential, cipher_credential)
    ):
        try:
            await load_shop_access_context(
                session,
                shop_binding_id=binding.id,
                key_ring=key_ring,
                now=current,
            )
        except (CommerceAccessBlocked, ValueError):
            common.append("BLOCKED_CREDENTIAL_DECRYPTION")

    image_keys = ("product.image.upload",)
    image_blockers = [
        *common,
        *_endpoint_evidence_blockers(
            endpoint_evidence,
            image_keys,
            legacy_flag="image_upload_verified",
            legacy_blocker="BLOCKED_UNVERIFIED_IMAGE_UPLOAD_ENDPOINT",
        ),
        *_scope_blockers(scopes, endpoint_evidence, image_keys),
    ]
    submission_keys: tuple[str, ...] = ()
    if listing_mode is not ListingMode.UNKNOWN:
        submission_keys = _product_create_chain_endpoint_keys(listing_mode)
    submission_blockers = [*common]
    if submission_keys:
        submission_blockers.extend(
            _endpoint_evidence_blockers(
                endpoint_evidence,
                submission_keys,
                legacy_flag="live_submission_validation_verified",
                legacy_blocker="BLOCKED_UNVERIFIED_LIVE_PRODUCT_VALIDATION",
            )
        )
        submission_blockers.extend(
            _scope_blockers(scopes, endpoint_evidence, submission_keys)
        )

    image_result = tuple(dict.fromkeys(image_blockers))
    submission_result = tuple(dict.fromkeys(submission_blockers))
    return ProductCapabilityDecision(
        platform_configured=platform_configured,
        master_key_configured=key_ring is not None,
        listing_mode=listing_mode,
        image_upload_enabled=not image_result,
        product_submission_enabled=bool(submission_keys) and not submission_result,
        image_upload_blockers=image_result,
        product_submission_blockers=submission_result,
    )