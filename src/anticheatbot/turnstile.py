from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class TurnstileUnavailableError(RuntimeError):
    """Siteverify could not establish a result; the user can retry a fresh challenge."""


async def verify_turnstile(
    *,
    secret: str,
    response_token: str,
    remote_ip: str | None = None,
    expected_hostname: str | None = None,
) -> bool:
    if not response_token:
        return False
    if len(response_token) > 2048:
        log.warning("turnstile response token exceeds max length")
        return False
    data: dict[str, Any] = {"secret": secret, "response": response_token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data=data,
            )
            r.raise_for_status()
            body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("turnstile siteverify unavailable: %s", type(e).__name__)
        raise TurnstileUnavailableError("siteverify unavailable") from e
    if not isinstance(body, dict) or not isinstance(body.get("success"), bool):
        raise TurnstileUnavailableError("invalid siteverify response")
    ok = body["success"] is True
    if not ok:
        codes = body.get("error-codes", [])
        if not isinstance(codes, list):
            raise TurnstileUnavailableError("invalid siteverify response")
        log.warning("turnstile failed: %s", codes)
        if any(code in codes for code in ("internal-error", "invalid-input-secret", "missing-input-secret")):
            raise TurnstileUnavailableError("siteverify unavailable")
    elif expected_hostname and str(body.get("hostname", "")).lower() != expected_hostname.lower():
        log.warning("turnstile hostname mismatch")
        return False
    return ok
