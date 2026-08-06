"""Optional Miaoshou ERP provider adapters."""

from app.integrations.miaoshou.client import (
    MiaoshouClient,
    MiaoshouClientError,
    MiaoshouConfig,
    MiaoshouConfigurationError,
    MiaoshouFailureCategory,
)
from app.integrations.miaoshou.shops import MiaoshouShopAdapter

__all__ = [
    "MiaoshouClient",
    "MiaoshouClientError",
    "MiaoshouConfig",
    "MiaoshouConfigurationError",
    "MiaoshouFailureCategory",
    "MiaoshouShopAdapter",
]