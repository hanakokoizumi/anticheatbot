from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


async def verify_turnstile(*, secret: str, response_token: str, remote_ip: str | None = None) -> bool:
    if not response_token:
        return False
    data: dict[str, Any] = {"secret": secret, "response": response_token}
    if remote_ip:
        data["remoteip"] = remote_ip
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=data,
        )
        r.raise_for_status()
        body = r.json()
    ok = bool(body.get("success"))
    if not ok:
        log.warning("turnstile failed: %s", body)
    return ok
