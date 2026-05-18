from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from anticheatbot.web.admin_api import _coerce_admin_settings_value
from anticheatbot.web.json_body import json_object


def test_coerce_verify_mode_whitelist() -> None:
    assert _coerce_admin_settings_value("verify_mode", "quiz") == "quiz"
    with pytest.raises(web.HTTPBadRequest):
        _coerce_admin_settings_value("verify_mode", "inject")


def test_coerce_spam_action_whitelist() -> None:
    assert _coerce_admin_settings_value("spam_action", "delete_and_restrict") == "delete_and_restrict"
    with pytest.raises(web.HTTPBadRequest):
        _coerce_admin_settings_value("spam_action", "ban_channel")


def test_coerce_verify_timeout_bounded() -> None:
    assert _coerce_admin_settings_value("verify_timeout_seconds", 120) == 120
    with pytest.raises(web.HTTPBadRequest):
        _coerce_admin_settings_value("verify_timeout_seconds", 10)
    with pytest.raises(web.HTTPBadRequest):
        _coerce_admin_settings_value("verify_timeout_seconds", True)


def test_coerce_welcome_max_length() -> None:
    _coerce_admin_settings_value("welcome_message_markdown", "x" * 4000)
    with pytest.raises(web.HTTPBadRequest):
        _coerce_admin_settings_value("welcome_message_markdown", "x" * 4001)


def test_json_object_rejects_large_content_length() -> None:
    req = MagicMock()
    req.content_length = 900_000

    async def run() -> None:
        await json_object(req)

    with pytest.raises(web.HTTPRequestEntityTooLarge):
        asyncio.run(run())
