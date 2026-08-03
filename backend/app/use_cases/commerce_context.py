"""Ephemeral authorization context shared by commerce application services."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.enums import AuthorizationStatus, ListingMode, Scope
from app.domain.scopes import ScopeSet


class CommerceAccessBlocked(PermissionError):
    """Raised before any platform request when shop capabilities are unsafe."""


@dataclass(frozen=True, slots=True)
class ShopAccessContext:
    shop_binding_id: str
    shop_id: str
    region: str
    listing_mode: ListingMode
    authorization_status: AuthorizationStatus
    scopes: ScopeSet
    access_token: str = field(repr=False)
    shop_cipher: str = field(repr=False)

    def __post_init__(self) -> None:
        required_text = {
            "shop binding id": self.shop_binding_id,
            "shop id": self.shop_id,
            "region": self.region,
            "access token": self.access_token,
            "shop cipher": self.shop_cipher,
        }
        if any(not value.strip() for value in required_text.values()):
            raise ValueError("shop access context contains an empty required value")

    def require_active(self) -> None:
        if self.authorization_status is not AuthorizationStatus.ACTIVE:
            raise CommerceAccessBlocked("shop authorization is not active")

    def require_scopes(self, *required: Scope) -> None:
        self.require_active()
        try:
            self.scopes.require(required)
        except PermissionError as exc:
            raise CommerceAccessBlocked(str(exc)) from exc

    def require_listing_write(self) -> ListingMode:
        self.require_active()
        if self.listing_mode is ListingMode.UNKNOWN:
            raise CommerceAccessBlocked("listing mode is not verified")
        return self.listing_mode