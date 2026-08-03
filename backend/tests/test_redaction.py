from __future__ import annotations

from shared.redaction import REDACTED, REDACTED_BODY, redact_mapping, redact_signature_body, redact_url


def test_recursive_redaction_removes_secrets_and_buyer_pii() -> None:
    source = {
        "access_token": "top-secret-token",
        "nested": {
            "Cookie": "sid=abc",
            "buyer_phone": "+60 12-345 6789",
            "safe": "inventory-ready",
        },
    }
    result = redact_mapping(source)
    assert result["access_token"] == REDACTED
    assert result["nested"]["Cookie"] == REDACTED  # type: ignore[index]
    assert result["nested"]["buyer_phone"] == REDACTED  # type: ignore[index]
    assert result["nested"]["safe"] == "inventory-ready"  # type: ignore[index]
    assert source["access_token"] == "top-secret-token"


def test_url_and_signature_body_are_never_logged() -> None:
    redacted = redact_url(
        "https://example.invalid/path?access_token=secret&cursor=page-1&email=a%40example.com#fragment"
    )
    assert "secret" not in redacted
    assert "a%40example.com" not in redacted
    assert "cursor=page-1" in redacted
    assert "fragment" not in redacted
    assert redact_signature_body(b'{"shop_cipher":"secret"}') == REDACTED_BODY