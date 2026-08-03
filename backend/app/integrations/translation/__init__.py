"""Translation provider contracts and Azure Translator implementation."""

from app.integrations.translation.azure import AzureTranslator, AzureTranslatorConfig
from app.integrations.translation.contracts import (
    MAX_TRANSLATION_CHARACTERS,
    MAX_TRANSLATION_ITEMS,
    SUPPORTED_LANGUAGES,
    TranslationConfigurationBlocked,
    TranslationError,
    TranslationProvider,
    TranslationRequest,
    TranslationRequestRejected,
    TranslationResult,
    TranslationUpstreamError,
)

__all__ = [
    "AzureTranslator",
    "AzureTranslatorConfig",
    "MAX_TRANSLATION_CHARACTERS",
    "MAX_TRANSLATION_ITEMS",
    "SUPPORTED_LANGUAGES",
    "TranslationConfigurationBlocked",
    "TranslationError",
    "TranslationProvider",
    "TranslationRequest",
    "TranslationRequestRejected",
    "TranslationResult",
    "TranslationUpstreamError",
]