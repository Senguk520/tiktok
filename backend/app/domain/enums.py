"""Closed enums for authorization, capabilities, listing and operation state."""

from enum import StrEnum


class AuthorizationStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    REFRESH_REQUIRED = "REFRESH_REQUIRED"
    DEAUTHORIZED = "DEAUTHORIZED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class Scope(StrEnum):
    AUTHORIZATION_INFO = "seller.authorization.info"
    PRODUCT_BASIC = "seller.product.basic"
    PRODUCT_WRITE = "seller.product.write"
    PRODUCT_DELETE = "seller.product.delete"
    ORDER_INFO = "seller.order.info"
    LOGISTICS = "seller.logistics"
    GLOBAL_PRODUCT_INFO = "seller.global_product.info"
    GLOBAL_PRODUCT_WRITE = "seller.global_product.write"
    GLOBAL_PRODUCT_DELETE = "seller.global_product.delete"


class ListingMode(StrEnum):
    LOCAL_REPLICATION = "LOCAL_REPLICATION"
    GLOBAL_LEGACY = "GLOBAL_LEGACY"
    UNKNOWN = "UNKNOWN"


class MarketProductStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SELLER_DEACTIVATED = "SELLER_DEACTIVATED"
    PLATFORM_DEACTIVATED = "PLATFORM_DEACTIVATED"
    FREEZE = "FREEZE"
    DELETED = "DELETED"
    FAILED = "FAILED"


class ProductDraftStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    ARCHIVED = "ARCHIVED"


class WriteState(StrEnum):
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    SUBMITTED = "SUBMITTED"
    AUDITING = "AUDITING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class OperationKind(StrEnum):
    CREATE = "CREATE"
    FULL_EDIT = "FULL_EDIT"
    PARTIAL_EDIT = "PARTIAL_EDIT"
    UPDATE_PRICE = "UPDATE_PRICE"
    UPDATE_INVENTORY = "UPDATE_INVENTORY"
    ACTIVATE = "ACTIVATE"
    DEACTIVATE = "DEACTIVATE"
    DELETE = "DELETE"

    @property
    def requires_audit(self) -> bool:
        return self in {self.CREATE, self.FULL_EDIT, self.PARTIAL_EDIT, self.ACTIVATE}


class OperationType(StrEnum):
    READ = "READ"
    WRITE = "WRITE"