"""Minimal allowlisted format for repository-safe live-check evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

SCHEMA_VERSION: Final[str] = "1"
LIVE_CHECK_FIELDS: Final[tuple[str, ...]] = (
    "schema_version",
    "checked_at_utc",
    "capability",
    "provider",
    "status",
    "site",
    "shop_count",
    "resource_fingerprints",
    "error_category",
    "notes",
)
_ALLOWED_STATUSES = frozenset({"PASSED", "BLOCKED", "FAILED"})
_ALLOWED_NOTES = frozenset(
    {
        "READ_ONLY_CHECK_SUCCEEDED",
        "PROVIDER_DISABLED",
        "LIVE_CREDENTIALS_UNAVAILABLE",
        "CONFIGURATION_BLOCKED",
        "PROVIDER_REQUEST_FAILED",
        "CHECK_EXECUTION_FAILED",
    }
)
_SAFE_ENUM = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_SITE = re.compile(r"^[A-Z0-9_]{2,32}$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{16}$")


class LiveCheckFormatError(ValueError):
    """Raised when evidence is not exactly the safe schema."""


def checked_at_utc(now: datetime | None = None) -> str:
    selected = datetime.now(UTC) if now is None else now
    if selected.tzinfo is None:
        raise LiveCheckFormatError("live-check time must include a timezone")
    return selected.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resource_fingerprint(*identity_parts: str) -> str:
    """Return a short domain-separated digest without retaining raw identities."""

    if not identity_parts or any(not isinstance(part, str) or not part for part in identity_parts):
        raise LiveCheckFormatError("resource fingerprint identity is unavailable")
    encoded = [part.encode("utf-8") for part in identity_parts]
    material = b"live-check-resource/v1\0" + b"".join(
        len(part).to_bytes(4, "big") + part for part in encoded
    )
    return f"sha256:{hashlib.sha256(material).hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
class LiveCheckReport:
    schema_version: str
    checked_at_utc: str
    capability: str
    provider: str
    status: str
    site: str
    shop_count: int
    resource_fingerprints: tuple[str, ...]
    error_category: str | None
    notes: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str)
            for value in (
                self.schema_version,
                self.checked_at_utc,
                self.capability,
                self.provider,
                self.status,
                self.site,
                self.notes,
            )
        ) or (self.error_category is not None and not isinstance(self.error_category, str)):
            raise LiveCheckFormatError("live-check field types are invalid")
        if self.schema_version != SCHEMA_VERSION:
            raise LiveCheckFormatError("unsupported live-check schema")
        try:
            parsed_time = datetime.fromisoformat(self.checked_at_utc.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LiveCheckFormatError("live-check time is invalid") from exc
        if (
            not self.checked_at_utc.endswith("Z")
            or parsed_time.tzinfo is None
            or parsed_time.utcoffset() != UTC.utcoffset(parsed_time)
            or checked_at_utc(parsed_time) != self.checked_at_utc
        ):
            raise LiveCheckFormatError("live-check time must be canonical UTC")
        if not _SAFE_ENUM.fullmatch(self.capability) or not _SAFE_ENUM.fullmatch(self.provider):
            raise LiveCheckFormatError("live-check capability or provider is invalid")
        if self.status not in _ALLOWED_STATUSES:
            raise LiveCheckFormatError("live-check status is invalid")
        if not _SAFE_SITE.fullmatch(self.site):
            raise LiveCheckFormatError("live-check site is invalid")
        if not isinstance(self.shop_count, int) or isinstance(self.shop_count, bool) or self.shop_count < 0:
            raise LiveCheckFormatError("live-check shop count is invalid")
        fingerprints = self.resource_fingerprints
        if (
            not isinstance(fingerprints, tuple)
            or tuple(sorted(set(fingerprints))) != fingerprints
            or any(
                not isinstance(value, str) or not _FINGERPRINT.fullmatch(value)
                for value in fingerprints
            )
            or self.shop_count != len(fingerprints)
        ):
            raise LiveCheckFormatError("live-check fingerprints are invalid")
        if self.error_category is not None and not _SAFE_ENUM.fullmatch(self.error_category):
            raise LiveCheckFormatError("live-check error category is invalid")
        if self.status == "PASSED" and self.error_category is not None:
            raise LiveCheckFormatError("passed live-check cannot contain an error")
        if self.status != "PASSED" and self.error_category is None:
            raise LiveCheckFormatError("non-passed live-check requires an error category")
        if self.notes not in _ALLOWED_NOTES:
            raise LiveCheckFormatError("live-check notes are not allowlisted")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "checked_at_utc": self.checked_at_utc,
            "capability": self.capability,
            "provider": self.provider,
            "status": self.status,
            "site": self.site,
            "shop_count": self.shop_count,
            "resource_fingerprints": list(self.resource_fingerprints),
            "error_category": self.error_category,
            "notes": self.notes,
        }


def report_from_mapping(value: Mapping[str, Any]) -> LiveCheckReport:
    if set(value) != set(LIVE_CHECK_FIELDS):
        raise LiveCheckFormatError("live-check fields do not match the allowlist")
    raw_fingerprints = value["resource_fingerprints"]
    if not isinstance(raw_fingerprints, Sequence) or isinstance(raw_fingerprints, (str, bytes)):
        raise LiveCheckFormatError("live-check fingerprints must be a sequence")
    try:
        return LiveCheckReport(
            schema_version=value["schema_version"],
            checked_at_utc=value["checked_at_utc"],
            capability=value["capability"],
            provider=value["provider"],
            status=value["status"],
            site=value["site"],
            shop_count=value["shop_count"],
            resource_fingerprints=tuple(raw_fingerprints),
            error_category=value["error_category"],
            notes=value["notes"],
        )
    except (TypeError, AttributeError) as exc:
        raise LiveCheckFormatError("live-check field types are invalid") from exc


def serialize_report(report: LiveCheckReport) -> bytes:
    if not isinstance(report, LiveCheckReport):
        raise LiveCheckFormatError("only validated live-check reports can be serialized")
    mapping = report.to_mapping()
    if tuple(mapping) != LIVE_CHECK_FIELDS:
        raise LiveCheckFormatError("live-check serializer field order changed")
    return (json.dumps(mapping, ensure_ascii=True, indent=2) + "\n").encode("utf-8")