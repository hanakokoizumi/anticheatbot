from __future__ import annotations

import pytest
from aiohttp import web

from anticheatbot.services.quiz import normalize_shuffle_order
from anticheatbot.web.admin_api import _coerce_quiz_question_dict, _strict_quiz_int
from anticheatbot.web.verify_api import _strict_answer_index


def test_strict_quiz_int_accepts_int() -> None:
    assert _strict_quiz_int(7, field="points", err_prefix="") == 7


def test_strict_quiz_int_accepts_integral_float() -> None:
    assert _strict_quiz_int(3.0, field="points", err_prefix="") == 3


@pytest.mark.parametrize("bad", [True, False, 1.5, "x", None])
def test_strict_quiz_int_rejects(bad: object) -> None:
    with pytest.raises(web.HTTPBadRequest):
        _strict_quiz_int(bad, field="points", err_prefix="")


def test_coerce_quiz_question_prefix_on_bad_int() -> None:
    with pytest.raises(web.HTTPBadRequest) as ei:
        _coerce_quiz_question_dict(
            {
                "prompt": "ok",
                "choices": ["a", "b"],
                "correct_index": 1.2,
            },
            label="q[2]",
        )
    assert "q[2]" in (ei.value.text or "")


def test_normalize_shuffle_order_identity_on_bad_map() -> None:
    assert normalize_shuffle_order(None, 1, 3) == [0, 1, 2]
    assert normalize_shuffle_order("x", 1, 3) == [0, 1, 2]


def test_normalize_shuffle_order_int_keys_and_valid_perm() -> None:
    m = {5: [2, 0, 1], "6": [0, 1]}  # type: ignore[dict-item]
    assert normalize_shuffle_order(m, 5, 3) == [2, 0, 1]
    assert normalize_shuffle_order(m, 6, 2) == [0, 1]


def test_normalize_shuffle_order_rejects_bad_permutation() -> None:
    assert normalize_shuffle_order({"1": [0, 0]}, 1, 2) == [0, 1]


def test_strict_answer_index_accepts_int_and_integral_float() -> None:
    assert _strict_answer_index(0) == 0
    assert _strict_answer_index(2.0) == 2


@pytest.mark.parametrize("bad", [True, False, 1.7])
def test_strict_answer_index_rejects(bad: object) -> None:
    with pytest.raises(TypeError):
        _strict_answer_index(bad)
