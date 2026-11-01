from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiohttp import web

from anticheatbot import turnstile
from anticheatbot.config import Settings
from anticheatbot.web.verify_api import _turnstile_or_403


def _mock_siteverify(monkeypatch, body=None, error=None):
    response = MagicMock()
    response.json.return_value = body
    client = AsyncMock()
    client.post.side_effect = error
    client.post.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr(turnstile.httpx, "AsyncClient", MagicMock(return_value=context))
    return client


def test_siteverify_accepts_matching_hostname(monkeypatch) -> None:
    client = _mock_siteverify(monkeypatch, {"success": True, "hostname": "verify.example.com"})
    assert asyncio.run(turnstile.verify_turnstile(
        secret="s", response_token="t", expected_hostname="verify.example.com"
    )) is True
    assert "remoteip" not in client.post.call_args.kwargs["data"]


def test_siteverify_rejects_other_hostname(monkeypatch) -> None:
    _mock_siteverify(monkeypatch, {"success": True, "hostname": "another.example.com"})
    assert asyncio.run(turnstile.verify_turnstile(
        secret="s", response_token="t", expected_hostname="verify.example.com"
    )) is False


def test_siteverify_rejects_replayed_token(monkeypatch) -> None:
    _mock_siteverify(monkeypatch, {"success": False, "error-codes": ["timeout-or-duplicate"]})
    assert asyncio.run(turnstile.verify_turnstile(secret="s", response_token="t")) is False


@pytest.mark.parametrize("body", [{"success": "false"}, [], {"success": False, "error-codes": ["internal-error"]}])
def test_siteverify_unusable_response_fails_closed(monkeypatch, body) -> None:
    _mock_siteverify(monkeypatch, body)
    with pytest.raises(turnstile.TurnstileUnavailableError):
        asyncio.run(turnstile.verify_turnstile(secret="s", response_token="t"))


def test_siteverify_network_error_is_retryable_api_error(monkeypatch) -> None:
    _mock_siteverify(monkeypatch, error=httpx.ReadTimeout("unavailable"))
    settings = Settings(BOT_TOKEN="1:x", WEBAPP_PUBLIC_URL="https://verify.example.com",
                        TURNSTILE_SITE_KEY="public", TURNSTILE_SECRET_KEY="private")
    group = MagicMock(turnstile_enabled=True)
    with pytest.raises(web.HTTPServiceUnavailable) as exc:
        asyncio.run(_turnstile_or_403(settings, group, "challenge", None))
    assert "turnstile_unavailable" in exc.value.text


@pytest.mark.parametrize("site,secret", [(None, "secret"), ("site", None), (None, None)])
def test_missing_configuration_fails_closed(site, secret) -> None:
    settings = Settings(BOT_TOKEN="1:x", WEBAPP_PUBLIC_URL="https://verify.example.com",
                        TURNSTILE_SITE_KEY=site, TURNSTILE_SECRET_KEY=secret)
    with pytest.raises(web.HTTPServiceUnavailable) as exc:
        asyncio.run(_turnstile_or_403(settings, MagicMock(turnstile_enabled=True), "challenge", None))
    assert "turnstile_not_configured" in exc.value.text
