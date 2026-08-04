"""Cryptographic primitives shared by the Core and Collector services.

This module owns protocol details only.  It never reads credentials from a
request body and it does not persist keys.  Callers provide an environment
value at process startup and keep the resulting key ring for that process.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_VALUE_RE = re.compile(r"^(?P<version>v[1-9][0-9]*):(?P<key>[A-Za-z0-9_-]+={0,2})$")
_WIRE_VALUE_RE = re.compile(
    r"^(?P<version>v[1-9][0-9]*):(?P<nonce>[A-Za-z0-9_-]+={0,2}):(?P<ciphertext>[A-Za-z0-9_-]+={0,2})$"
)
INTERNAL_HMAC_SECRET_ENV = "COLLECTOR_INTERNAL_HMAC_SECRET"
INTERNAL_HMAC_TIMESTAMP_HEADER = "X-Internal-Timestamp"
INTERNAL_HMAC_SIGNATURE_HEADER = "X-Internal-Signature"


class SecurityConfigurationError(ValueError):
    """Raised for malformed or unsafe cryptographic configuration."""


class AuthenticationError(ValueError):
    """Raised when authenticated ciphertext or an internal signature is invalid."""


def _decode_base64(value: str, *, label: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise SecurityConfigurationError(f"{label} must be non-empty base64")
    # URL-safe values are used in environment variables and wire values.  Add
    # only legal padding; validate the decoded alphabet and round-trip shape.
    if len(value) % 4 == 1:
        raise SecurityConfigurationError(f"{label} has invalid base64 length")
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
        raise SecurityConfigurationError(f"{label} is not valid base64") from exc
    if not decoded:
        raise SecurityConfigurationError(f"{label} must not be empty")
    return decoded


def _encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


@dataclass(frozen=True, slots=True)
class MasterKey:
    """A versioned AES-256-GCM key loaded from one environment value."""

    version: str
    key: bytes

    def __post_init__(self) -> None:
        if not re.fullmatch(r"v[1-9][0-9]*", self.version):
            raise SecurityConfigurationError("key version must look like v1")
        if len(self.key) not in {16, 24, 32}:
            raise SecurityConfigurationError("AES key must be 128, 192, or 256 bits")


def parse_master_key(value: str, *, expected_version: str | None = None) -> MasterKey:
    """Parse ``vN:<urlsafe-base64-key>`` from an environment variable."""

    if not isinstance(value, str):
        raise SecurityConfigurationError("master key must be text")
    match = _KEY_VALUE_RE.fullmatch(value.strip())
    if match is None:
        raise SecurityConfigurationError("master key must use vN:base64 format")
    version = match.group("version")
    if expected_version is not None and version != expected_version:
        raise SecurityConfigurationError("master key version is not accepted")
    key = _decode_base64(match.group("key"), label="master key")
    return MasterKey(version=version, key=key)


def load_master_key_from_env(
    env: Mapping[str, str] | None = None,
    *,
    variable: str = "APP_MASTER_KEY",
    expected_version: str | None = None,
) -> MasterKey:
    """Load a versioned master key without ever logging its value."""

    values = os.environ if env is None else env
    value = values.get(variable)
    if value is None or not value.strip():
        raise SecurityConfigurationError(f"missing environment key: {variable}")
    return parse_master_key(value, expected_version=expected_version)


@dataclass(frozen=True, slots=True)
class EncryptedValue:
    """A self-describing AES-GCM value safe to store as opaque text."""

    key_version: str
    nonce: bytes
    ciphertext: bytes

    def __post_init__(self) -> None:
        if not re.fullmatch(r"v[1-9][0-9]*", self.key_version):
            raise SecurityConfigurationError("ciphertext key version is invalid")
        if len(self.nonce) != 12:
            raise SecurityConfigurationError("AES-GCM nonce must be 12 bytes")
        if not self.ciphertext:
            raise SecurityConfigurationError("ciphertext must not be empty")

    def encode(self) -> str:
        return f"{self.key_version}:{_encode_base64(self.nonce)}:{_encode_base64(self.ciphertext)}"

    @classmethod
    def decode(cls, value: str) -> EncryptedValue:
        if not isinstance(value, str):
            raise AuthenticationError("ciphertext must be text")
        match = _WIRE_VALUE_RE.fullmatch(value)
        if match is None:
            raise AuthenticationError("ciphertext has invalid envelope")
        try:
            nonce = _decode_base64(match.group("nonce"), label="ciphertext nonce")
            ciphertext = _decode_base64(match.group("ciphertext"), label="ciphertext body")
        except SecurityConfigurationError as exc:
            raise AuthenticationError("ciphertext has invalid envelope") from exc
        try:
            return cls(key_version=match.group("version"), nonce=nonce, ciphertext=ciphertext)
        except SecurityConfigurationError as exc:
            raise AuthenticationError("ciphertext has invalid envelope") from exc


@dataclass(frozen=True, slots=True)
class KeyRing:
    """Immutable key lookup used for decrypting current and rotated values."""

    keys: Mapping[str, MasterKey]

    def __post_init__(self) -> None:
        if not self.keys:
            raise SecurityConfigurationError("key ring must contain one key")
        for version, key in self.keys.items():
            if version != key.version:
                raise SecurityConfigurationError("key ring version does not match key")

    @classmethod
    def from_current(cls, key: MasterKey) -> KeyRing:
        return cls(keys={key.version: key})

    def get(self, version: str) -> MasterKey:
        try:
            return self.keys[version]
        except KeyError as exc:
            raise AuthenticationError("ciphertext key version is unavailable") from exc


def encrypt_value(
    plaintext: str | bytes,
    key: MasterKey,
    *,
    aad: str | bytes = b"tiktok-single-shop/v1",
    nonce: bytes | None = None,
) -> EncryptedValue:
    """Encrypt one value with AES-GCM and explicit additional authenticated data."""

    body = plaintext.encode("utf-8") if isinstance(plaintext, str) else bytes(plaintext)
    associated_data = aad.encode("utf-8") if isinstance(aad, str) else bytes(aad)
    chosen_nonce = os.urandom(12) if nonce is None else bytes(nonce)
    if len(chosen_nonce) != 12:
        raise SecurityConfigurationError("AES-GCM nonce must be exactly 12 bytes")
    ciphertext = AESGCM(key.key).encrypt(chosen_nonce, body, associated_data)
    return EncryptedValue(key_version=key.version, nonce=chosen_nonce, ciphertext=ciphertext)


def decrypt_value(
    encrypted: EncryptedValue | str,
    key_ring: KeyRing,
    *,
    aad: str | bytes = b"tiktok-single-shop/v1",
) -> bytes:
    """Decrypt and authenticate a value, failing closed for every mismatch."""

    envelope = EncryptedValue.decode(encrypted) if isinstance(encrypted, str) else encrypted
    key = key_ring.get(envelope.key_version)
    associated_data = aad.encode("utf-8") if isinstance(aad, str) else bytes(aad)
    try:
        return AESGCM(key.key).decrypt(envelope.nonce, envelope.ciphertext, associated_data)
    except InvalidTag as exc:
        raise AuthenticationError("ciphertext authentication failed") from exc


def decrypt_text(
    encrypted: EncryptedValue | str,
    key_ring: KeyRing,
    *,
    aad: str | bytes = b"tiktok-single-shop/v1",
) -> str:
    """Decrypt a UTF-8 value and reject invalid text rather than replacing bytes."""

    try:
        return decrypt_value(encrypted, key_ring, aad=aad).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthenticationError("decrypted value is not UTF-8") from exc


def load_internal_hmac_secret_from_env(
    env: Mapping[str, str] | None = None,
    *,
    variable: str = INTERNAL_HMAC_SECRET_ENV,
) -> bytes:
    """Load a strong process-to-process HMAC secret without exposing its value."""

    values = os.environ if env is None else env
    value = values.get(variable, "")
    try:
        secret = value.encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise SecurityConfigurationError("internal HMAC secret is invalid") from exc
    if (
        len(secret) < 32
        or len(secret) > 4096
        or any(character < 32 or character == 127 for character in secret)
    ):
        raise SecurityConfigurationError(
            f"missing or invalid environment key: {variable}"
        )
    return secret


def _internal_message(
    *,
    timestamp: int,
    method: str,
    path: str,
    body: bytes,
) -> bytes:
    if timestamp < 0 or len(str(timestamp)) != 10:
        raise AuthenticationError("internal timestamp must be a 10-digit Unix time")
    normalized_method = method.upper().strip()
    normalized_path = path.strip()
    if not normalized_method or not normalized_path.startswith("/"):
        raise AuthenticationError("internal method or path is invalid")
    # Length-prefixing prevents ambiguity between method/path/body boundaries.
    parts = (str(timestamp).encode("ascii"), normalized_method.encode("utf-8"), normalized_path.encode("utf-8"), body)
    return b"internal-v1\0" + b"".join(len(part).to_bytes(4, "big") + part for part in parts)


def sign_internal_message(
    secret: bytes | str,
    *,
    timestamp: int,
    method: str,
    path: str,
    body: bytes = b"",
) -> str:
    """Create a hex HMAC-SHA256 signature for the localhost service boundary."""

    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if not secret_bytes:
        raise SecurityConfigurationError("internal HMAC secret must not be empty")
    message = _internal_message(timestamp=timestamp, method=method, path=path, body=bytes(body))
    return hmac.new(secret_bytes, message, hashlib.sha256).hexdigest()


def verify_internal_message(
    secret: bytes | str,
    signature: str,
    *,
    timestamp: int | str,
    method: str,
    path: str,
    body: bytes = b"",
    now: int | float | datetime | None = None,
    max_age_seconds: int = 300,
    max_future_seconds: int = 30,
) -> bool:
    """Verify timestamp freshness and HMAC in constant time.

    A boolean result avoids exposing whether a caller failed on timestamp or
    signature.  A caller should reject the request whenever this returns false.
    Replay storage belongs to the later idempotency layer; this primitive only
    enforces a short validity window.
    """

    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
        return False
    try:
        numeric_timestamp = int(timestamp)
    except (TypeError, ValueError):
        return False
    if str(numeric_timestamp) != str(timestamp) or len(str(numeric_timestamp)) != 10:
        return False
    if max_age_seconds < 0 or max_future_seconds < 0:
        return False
    if isinstance(now, datetime):
        current_time = now.timestamp()
    elif now is None:
        current_time = time.time()
    else:
        current_time = float(now)
    age = current_time - numeric_timestamp
    if age > max_age_seconds or age < -max_future_seconds:
        return False
    try:
        expected = sign_internal_message(
            secret,
            timestamp=numeric_timestamp,
            method=method,
            path=path,
            body=body,
        )
    except (AuthenticationError, SecurityConfigurationError, TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)


def utc_timestamp(now: datetime | None = None) -> int:
    """Return a 10-digit Unix timestamp for signing without local-time ambiguity."""

    value = datetime.now(UTC) if now is None else now
    return int(value.timestamp())


# Names used by service code are explicit aliases, not separate protocols.
internal_hmac = sign_internal_message
verify_internal_hmac = verify_internal_message