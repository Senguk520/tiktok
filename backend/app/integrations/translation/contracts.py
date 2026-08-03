"""Strict value contracts for backend-only translation providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

SUPPORTED_LANGUAGES = frozenset({"zh-Hans", "en", "ms"})
MAX_TRANSLATION_ITEMS = 25
MAX_TRANSLATION_CHARACTERS = 5_000


class TranslationError(RuntimeError):
    """Stable base failure that never includes provider response content."""


class TranslationConfigurationBlocked(TranslationError):
    pass


class TranslationRequestRejected(TranslationError, ValueError):
    pass


class TranslationUpstreamError(TranslationError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def normalize_language(value: str) -> str:
    normalized = value.strip()
    if normalized.lower() == "zh-hans":
        return "zh-Hans"
    if normalized.lower() in {"en", "ms"}:
        return normalized.lower()
    raise TranslationRequestRejected("translation language is not enabled")


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    texts: tuple[str, ...]
    source_language: str
    target_language: str

    def __post_init__(self) -> None:
        source = normalize_language(self.source_language)
        target = normalize_language(self.target_language)
        if source == target:
            raise TranslationRequestRejected("source and target languages must differ")
        if not 1 <= len(self.texts) <= MAX_TRANSLATION_ITEMS:
            raise TranslationRequestRejected("translation request must contain 1-25 texts")
        cleaned = tuple(text.strip() for text in self.texts)
        if any(not text for text in cleaned):
            raise TranslationRequestRejected("translation texts cannot be empty")
        if sum(len(text) for text in cleaned) > MAX_TRANSLATION_CHARACTERS:
            raise TranslationRequestRejected("translation request exceeds 5000 characters")
        object.__setattr__(self, "texts", cleaned)
        object.__setattr__(self, "source_language", source)
        object.__setattr__(self, "target_language", target)


@dataclass(frozen=True, slots=True)
class TranslationResult:
    texts: tuple[str, ...]
    source_language: str
    target_language: str
    provider: str
    request_id: str | None = None


class TranslationProvider(Protocol):
    async def translate(self, request: TranslationRequest) -> TranslationResult: ...