from __future__ import annotations

import time

import pytest

from anticheatbot.init_data import InitDataInvalidError, parse_language_code, parse_user_id, validate_init_data

from tests.signing import sign_webapp_init_data


def test_validate_init_data_ok() -> None:
    token = "123456:AA" + "a" * 30
    raw = sign_webapp_init_data(bot_token=token, user={"id": 4242, "language_code": "ja"})
    out = validate_init_data(raw, bot_token=token, max_age_seconds=3600)
    assert out["auth_date"] > 0
    assert out["user"]["id"] == 4242
    assert parse_user_id(out) == 4242
    assert parse_language_code(out) == "ja"


def test_validate_init_data_bad_hash() -> None:
    token = "1:fake"
    raw = "auth_date=1&user=%7B%22id%22%3A1%7D&hash=deadbeef"
    with pytest.raises(InitDataInvalidError, match="bad hash"):
        validate_init_data(raw, bot_token=token, max_age_seconds=3600)


def test_validate_init_data_too_old() -> None:
    token = "123456:AA" + "b" * 30
    old = int(time.time()) - 10_000
    raw = sign_webapp_init_data(bot_token=token, user={"id": 1}, auth_date=old)
    with pytest.raises(InitDataInvalidError, match="too old"):
        validate_init_data(raw, bot_token=token, max_age_seconds=60)


def test_parse_user_id_missing() -> None:
    with pytest.raises(InitDataInvalidError, match="missing user id"):
        parse_user_id({"user": {}})


def test_parse_user_id_bad_type() -> None:
    with pytest.raises(InitDataInvalidError, match="bad user id"):
        parse_user_id({"user": {"id": "not-an-int"}})
