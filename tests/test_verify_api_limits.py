from __future__ import annotations

import asyncio
import secrets
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from anticheatbot.turnstile import verify_turnstile
from anticheatbot.web.verify_api import _parse_verify_token_body, _parse_verify_token_query


def test_parse_verify_token_query_valid() -> None:
    tok = secrets.token_urlsafe(24)
    assert _parse_verify_token_query(tok) == tok


def test_parse_verify_token_query_rejects_oversized() -> None:
    with pytest.raises(web.HTTPForbidden) as ei:
        _parse_verify_token_query("x" * 65)
    assert ei.value.text == "bad token"


def test_parse_verify_token_query_rejects_bad_charset() -> None:
    with pytest.raises(web.HTTPForbidden) as ei:
        _parse_verify_token_query("ab/cd!efghijklmnopqrstuvwxyz12")
    assert ei.value.text == "bad token"


def test_parse_verify_token_body_rejects_bool() -> None:
    with pytest.raises(web.HTTPBadRequest) as ei:
        _parse_verify_token_body(True)
    assert ei.value.text == "missing token"


@pytest.mark.parametrize("factory", [_parse_verify_token_query, _parse_verify_token_body])
def test_parse_verify_token_missing(factory) -> None:
    if factory is _parse_verify_token_query:
        with pytest.raises(web.HTTPBadRequest) as ei:
            factory(None)
        assert ei.value.text == "missing t"
        with pytest.raises(web.HTTPBadRequest) as ei2:
            factory("   ")
        assert ei2.value.text == "missing t"
    else:
        with pytest.raises(web.HTTPBadRequest) as ei:
            factory(None)
        assert ei.value.text == "missing token"


def test_turnstile_rejects_huge_response_token() -> None:
    async def run() -> bool:
        return await verify_turnstile(secret="s", response_token="t" * 5000)

    assert asyncio.run(run()) is False


def test_turnstile_client_ip_ignores_xff_by_default() -> None:
    from anticheatbot.config import Settings
    from anticheatbot.web.verify_api import _turnstile_client_ip

    s = Settings(BOT_TOKEN="1:a", WEBAPP_PUBLIC_URL="https://x")
    req = MagicMock()
    req.headers = {"X-Forwarded-For": "203.0.113.9"}
    req.remote = "8.8.8.8"
    assert _turnstile_client_ip(req, s) == "8.8.8.8"


def test_turnstile_client_ip_uses_xff_when_trusted_and_valid() -> None:
    from anticheatbot.config import Settings
    from anticheatbot.web.verify_api import _turnstile_client_ip

    s = Settings(BOT_TOKEN="1:a", WEBAPP_PUBLIC_URL="https://x", trust_x_forwarded_for=True)
    req = MagicMock()
    req.headers = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
    req.remote = "127.0.0.1"
    assert _turnstile_client_ip(req, s) == "203.0.113.9"


def test_turnstile_client_ip_invalid_xff_falls_back_to_remote() -> None:
    from anticheatbot.config import Settings
    from anticheatbot.web.verify_api import _turnstile_client_ip

    s = Settings(BOT_TOKEN="1:a", WEBAPP_PUBLIC_URL="https://x", trust_x_forwarded_for=True)
    req = MagicMock()
    req.headers = {"X-Forwarded-For": "not-an-ip-address"}
    req.remote = "10.0.0.2"
    assert _turnstile_client_ip(req, s) is None
