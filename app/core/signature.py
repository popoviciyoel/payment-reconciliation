"""
Webhook signature verification.

The provider signs a *canonical* JSON form of the payload, not the raw body:
  - keys sorted alphabetically
  - compact separators (no whitespace)
  - `sandbox_id` excluded (it is added after signing)

Header format: `X-Webhook-Signature: sha256=<hex_digest>`
"""

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import HTTPException, Request, status

from app.config import settings

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Webhook-Signature"
SIGNATURE_PREFIX = "sha256="
UNSIGNED_FIELDS = frozenset({"sandbox_id"})


def canonical_json(payload: dict[str, Any], *, ensure_ascii: bool = True) -> bytes:
    """Serialize the signed portion of a payload in canonical form."""
    signed = {k: v for k, v in payload.items() if k not in UNSIGNED_FIELDS}
    return json.dumps(
        signed, sort_keys=True, separators=(",", ":"), ensure_ascii=ensure_ascii
    ).encode("utf-8")


def compute_signature(payload: dict[str, Any], secret: str, *, ensure_ascii: bool = True) -> str:
    """Return the expected header value (`sha256=<hex>`) for a payload."""
    digest = hmac.new(
        secret.encode("utf-8"),
        canonical_json(payload, ensure_ascii=ensure_ascii),
        hashlib.sha256,
    ).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(payload: dict[str, Any], header_value: str | None, secret: str) -> bool:
    """
    Constant-time check of the signature header against the payload.

    The provider's handling of non-ASCII characters (escaped `\\uXXXX` vs raw
    UTF-8) is undocumented, so both canonical forms are accepted. Both are
    HMACs keyed with our secret, so this does not weaken verification.
    """
    if not header_value or not header_value.startswith(SIGNATURE_PREFIX):
        return False

    candidates = [compute_signature(payload, secret, ensure_ascii=True)]
    non_ascii = compute_signature(payload, secret, ensure_ascii=False)
    if non_ascii != candidates[0]:
        candidates.append(non_ascii)

    # Evaluate every candidate (no short-circuit) to keep timing uniform.
    results = [hmac.compare_digest(header_value, c) for c in candidates]
    return any(results)


async def verified_payload(request: Request) -> dict[str, Any]:
    """
    FastAPI dependency: parse the JSON body and verify its signature.

    Raises 500 if the server has no secret configured (fail closed),
    400 for a malformed body, 401 for a missing/invalid signature.
    """
    secret = settings.webhook_secret
    if not secret:
        logger.error("WEBHOOK_SECRET is not configured; rejecting webhook")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Webhook secret not configured")

    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be a JSON object")

    if not verify_signature(payload, request.headers.get(SIGNATURE_HEADER), secret):
        logger.warning("Rejected webhook with invalid signature on %s", request.url.path)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")

    return payload
