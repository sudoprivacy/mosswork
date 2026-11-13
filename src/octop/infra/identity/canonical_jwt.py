"""Shared retained HS256 wire codec; credential-family policy lives in consumers.

This leaf has no configuration, DB, issuer, identity lookup or default secret.
Trusted existing authorities supply their own secret and original claims.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any


def node_utf8(value: str) -> bytes:
    """Match Node Buffer.from(str): replace unpaired UTF-16 surrogates by U+FFFD."""
    return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace").encode("utf-8")


def _invalid_json_constant(value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def _decode_object(encoded: str) -> dict[str, Any]:
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    value: object = json.loads(raw.decode("utf-8"), parse_constant=_invalid_json_constant)
    if not isinstance(value, dict):
        raise ValueError("JWT JSON object required")
    return value


def sign_claims(claims: dict[str, object], secret: bytes) -> str:
    """Sign original claims without adding identity, audience, time or JTI."""

    def encode(value: dict[str, object]) -> str:
        # JSON.stringify escapes lone surrogates, while preserving actual Unicode.
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8", "backslashreplace"
        )
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    signing_input = encode({"alg": "HS256", "typ": "JWT"}) + "." + encode(claims)
    digest = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return signing_input + "." + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verified_payload(token: str, secret: bytes) -> dict[str, Any] | None:
    """Verify HS256/typ/signature only; never treat this as an authenticated user."""
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        return None
    encoded_header, encoded_payload, signature = parts
    try:
        header = _decode_object(encoded_header)
        payload = _decode_object(encoded_payload)
        if header.get("alg") != "HS256" or header.get("typ") != "JWT":
            return None
        digest = hmac.new(
            secret, f"{encoded_header}.{encoded_payload}".encode(), hashlib.sha256
        ).digest()
        expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return payload if hmac.compare_digest(signature, expected) else None
    except (ValueError, TypeError, OverflowError, UnicodeError, binascii.Error):
        return None
