"""Test-only helpers (signing WebApp initData, etc.)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any


def sign_webapp_init_data(
    *,
    bot_token: str,
    user: dict[str, Any] | None,
    auth_date: int | None = None,
) -> str:
    """Build a valid Telegram WebApp initData query string (for tests)."""
    if auth_date is None:
        auth_date = int(time.time())
    pairs: dict[str, str] = {"auth_date": str(auth_date)}
    if user is not None:
        pairs["user"] = json.dumps(user, separators=(",", ":"))
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    sk = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    digest = hmac.new(sk, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    pairs["hash"] = digest
    return "&".join(f"{k}={pairs[k]}" for k in sorted(pairs))
