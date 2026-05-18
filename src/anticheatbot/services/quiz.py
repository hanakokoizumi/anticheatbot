from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from anticheatbot.db.models import GroupSettings, QuizQuestion, VerifyQuizSession


class QuizError(RuntimeError):
    pass


def normalize_shuffle_order(raw_maps: object, qid: int, n: int) -> list[int]:
    """Return permutation ``order`` where ``order[i]`` is the original choice index shown at display slot ``i``.

    Accepts JSON/ORM quirks: non-dict maps, int object keys, numeric strings in the list. Invalid data falls
    back to identity ``[0..n-1]`` (same grading rule as display).
    """
    if n <= 0:
        return []
    if not isinstance(raw_maps, dict):
        return list(range(n))
    maps: dict[str, Any] = {str(k): v for k, v in raw_maps.items()}
    raw_order = maps.get(str(qid))
    if not isinstance(raw_order, list) or len(raw_order) != n:
        return list(range(n))
    try:
        order = [int(x) for x in raw_order]
    except (TypeError, ValueError):
        return list(range(n))
    if sorted(order) != list(range(n)):
        return list(range(n))
    return order


def effective_quiz_draw_size(*, bank_size: int, draw_requested: int) -> int:
    """How many questions to draw: never more than the bank size, never below 1 when bank is non-empty."""
    if bank_size <= 0:
        return 0
    try:
        dr = int(draw_requested)
    except (TypeError, ValueError):
        dr = bank_size
    if dr < 1:
        dr = 1
    return min(bank_size, dr)


@dataclass
class DrawnQuiz:
    questions: list[QuizQuestion]
    shuffle_maps: dict[str, list[int]]


async def draw_quiz_for_token(
    session: AsyncSession,
    *,
    chat_id: int,
    token: str,
    gs: GroupSettings,
) -> DrawnQuiz:
    stmt = select(QuizQuestion).where(QuizQuestion.chat_id == chat_id)
    all_q = list((await session.scalars(stmt)).all())
    k = effective_quiz_draw_size(bank_size=len(all_q), draw_requested=gs.quiz_draw_count)
    if k == 0:
        raise QuizError("题库为空，无法开考")

    picked = list(random.sample(all_q, k=k))
    # 题目顺序也在会话内固定为随机排列（与选项打乱独立）
    random.shuffle(picked)
    ids = [q.id for q in picked]
    shuffle_maps: dict[str, list[int]] = {}
    for q in picked:
        n = len(q.choices_json)
        order = list(range(n))
        random.shuffle(order)
        shuffle_maps[str(q.id)] = order
    session.add(
        VerifyQuizSession(
            token=token,
            question_ids_json=ids,
            shuffle_maps_json=shuffle_maps,
        )
    )
    await session.flush()
    return DrawnQuiz(questions=picked, shuffle_maps=shuffle_maps)


async def grade_answers(
    session: AsyncSession,
    *,
    token: str,
    answers: dict[int, int],
) -> tuple[int, int]:
    """Return (num_correct, num_questions). Display indices are mapped via shuffle_maps_json."""
    sess = await session.get(VerifyQuizSession, token)
    if sess is None:
        raise QuizError("quiz session missing")
    ids = list(sess.question_ids_json)
    correct_count = 0
    total = 0
    for qid in ids:
        q = await session.get(QuizQuestion, qid)
        if q is None:
            continue
        total += 1
        n = len(q.choices_json)
        order = normalize_shuffle_order(sess.shuffle_maps_json, int(qid), n)
        ans = answers.get(qid)
        if ans is None:
            continue
        if ans < 0 or ans >= len(order):
            continue
        orig_pick = order[ans]
        if orig_pick == q.correct_index:
            correct_count += 1
    return correct_count, total
