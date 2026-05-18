from anticheatbot.services.quiz import effective_quiz_draw_size


def test_effective_quiz_draw_size_caps_by_bank() -> None:
    assert effective_quiz_draw_size(bank_size=3, draw_requested=10) == 3
    assert effective_quiz_draw_size(bank_size=10, draw_requested=10) == 10
    assert effective_quiz_draw_size(bank_size=100, draw_requested=5) == 5


def test_effective_quiz_draw_size_empty_bank() -> None:
    assert effective_quiz_draw_size(bank_size=0, draw_requested=10) == 0


def test_effective_quiz_draw_size_bad_requested_falls_back() -> None:
    assert effective_quiz_draw_size(bank_size=5, draw_requested=0) == 1
    assert effective_quiz_draw_size(bank_size=5, draw_requested=-3) == 1
