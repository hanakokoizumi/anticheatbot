from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl


class InitDataInvalidError(ValueError):
    pass


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def validate_init_data(init_data: str, *, bot_token: str, max_age_seconds: int) -> dict[str, Any]:
    """Validate Telegram WebApp initData string; returns parsed fields including user dict."""
    if not init_data:
        raise InitDataInvalidError("empty init_data")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataInvalidError("missing hash")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    sk = _secret_key(bot_token)
    digest = hmac.new(sk, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, received_hash):
        raise InitDataInvalidError("bad hash")
    auth_raw = pairs.get("auth_date")
    if not auth_raw:
        raise InitDataInvalidError("missing auth_date")
    try:
        auth_date = int(auth_raw)
    except ValueError as e:
        raise InitDataInvalidError("bad auth_date") from e
    if max_age_seconds > 0 and int(time.time()) - auth_date > max_age_seconds:
        raise InitDataInvalidError("auth_date too old")
    user_raw = pairs.get("user")
    user: dict[str, Any] | None
    if user_raw:
        try:
            user = json.loads(user_raw)
        except json.JSONDecodeError as e:
            raise InitDataInvalidError("bad user json") from e
    else:
        user = None
    return {"user": user, "auth_date": auth_date, "raw": pairs}


def parse_user_id(init_data_fields: dict[str, Any]) -> int:
    user = init_data_fields.get("user") or {}
    uid = user.get("id")
    if uid is None:
        raise InitDataInvalidError("missing user id")
    try:
        return int(uid)
    except (TypeError, ValueError) as e:
        raise InitDataInvalidError("bad user id") from e


def parse_language_code(init_data_fields: dict[str, Any]) -> str | None:
    user = init_data_fields.get("user") or {}
    lc = user.get("language_code")
    return str(lc) if lc else None
