from __future__ import annotations

from unittest.mock import MagicMock

from anticheatbot.bot_i18n import bot_t
from anticheatbot.web.verify_api import _welcome_template_for_user


def test_welcome_template_custom_trimmed() -> None:
    gs = MagicMock()
    gs.welcome_message_markdown = "  自定义 {mention}  "
    init = {"user": {"language_code": "en"}}
    assert _welcome_template_for_user(gs, init) == "自定义 {mention}"


def test_welcome_template_empty_uses_user_language() -> None:
    gs = MagicMock()
    gs.welcome_message_markdown = ""
    init = {"user": {"language_code": "ja"}}
    assert _welcome_template_for_user(gs, init) == bot_t("ja", "default_welcome")
