from __future__ import annotations

import base64

import pytest

from shared.security import (
    AuthenticationError,
    EncryptedValue,
    KeyRing,
    decrypt_text,
    encrypt_value,
    parse_master_key,
    sign_internal_message,
    verify_internal_message,
)


def _key_value(byte: int = 7) -> str:
    encoded = base64.urlsafe_b64encode(bytes([byte]) * 32).decode("ascii").rstrip("=")
    return f"v1:{encoded}"


def test_aes_gcm_round_trip_and_aad_binding() -> None:
    key = parse_master_key(_key_value())
    encrypted = encrypt_value("refresh-token", key, aad="credential:shop-1")
    assert decrypt_text(encrypted.encode(), KeyRing.from_current(key), aad="credential:shop-1") == (
        "refresh-token"
    )
    with pytest.raises(AuthenticationError):
        decrypt_text(encrypted.encode(), KeyRing.from_current(key), aad="credential:shop-2")


def test_aes_gcm_rejects_ciphertext_tampering() -> None:
    key = parse_master_key(_key_value())
    encrypted = encrypt_value("secret", key)
    changed = EncryptedValue(
        key_version=encrypted.key_version,
        nonce=encrypted.nonce,
        ciphertext=encrypted.ciphertext[:-1] + bytes([encrypted.ciphertext[-1] ^ 1]),
    )
    with pytest.raises(AuthenticationError):
        decrypt_text(changed, KeyRing.from_current(key))


def test_internal_hmac_binds_timestamp_method_path_and_body() -> None:
    timestamp = 1_750_000_000
    signature = sign_internal_message(
        "internal-secret",
        timestamp=timestamp,
        method="POST",
        path="/internal/v1/jobs",
        body=b'{"job_id":"123"}',
    )
    assert verify_internal_message(
        "internal-secret",
        signature,
        timestamp=timestamp,
        method="POST",
        path="/internal/v1/jobs",
        body=b'{"job_id":"123"}',
        now=timestamp + 5,
    )
    assert not verify_internal_message(
        "internal-secret",
        signature,
        timestamp=timestamp,
        method="POST",
        path="/internal/v1/jobs",
        body=b'{"job_id":"456"}',
        now=timestamp + 5,
    )
    assert not verify_internal_message(
        "internal-secret",
        signature,
        timestamp=timestamp,
        method="POST",
        path="/internal/v1/jobs",
        body=b'{"job_id":"123"}',
        now=timestamp + 301,
    )