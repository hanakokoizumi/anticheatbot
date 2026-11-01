from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from anticheatbot.db.models import GroupSettings, QuizQuestion, VerifyQuizSession


class QuizError(RuntimeError):
    pass


class QuizChangedError(QuizError):
    """The browser must reload before grading a missing or legacy question snapshot."""


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
    shuffle_maps: dict[str, Any]
    version: str
    required_correct: int


def quiz_from_snapshot(row: VerifyQuizSession) -> DrawnQuiz | None:
    maps = row.shuffle_maps_json or {}
    snapshot = maps.get("__snapshot_v1") if isinstance(maps, dict) else None
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("version"), str):
        return None
    records = snapshot.get("questions")
    required_correct = snapshot.get("required_correct")
    if (
        not isinstance(records, list) or not records
        or type(required_correct) is not int or not 1 <= required_correct <= len(records)
    ):
        return None
    questions: list[QuizQuestion] = []
    for record in records:
        if not isinstance(record, dict):
            return None
        qid, prompt, choices, correct = (record.get(k) for k in ("id", "prompt", "choices", "correct_index"))
        if (
            type(qid) is not int or not isinstance(prompt, str)
            or not isinstance(choices, list) or len(choices) < 2
            or not all(isinstance(choice, str) for choice in choices)
            or type(correct) is not int or not 0 <= correct < len(choices)
        ):
            return None
        # Transient ORM objects let display/translation use exactly the saved content without DB reads.
        questions.append(QuizQuestion(id=qid, prompt=prompt, choices_json=choices, correct_index=correct, points=1))
    if [q.id for q in questions] != list(row.question_ids_json):
        return None
    return DrawnQuiz(
        questions=questions, shuffle_maps=dict(maps), version=snapshot["version"], required_correct=required_correct
    )


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
    shuffle_maps: dict[str, Any] = {}
    for q in picked:
        n = len(q.choices_json)
        order = list(range(n))
        random.shuffle(order)
        shuffle_maps[str(q.id)] = order
    version = secrets.token_urlsafe(16)
    required_correct = min(max(1, gs.quiz_pass_min_correct), len(picked))
    shuffle_maps["__snapshot_v1"] = {
        "version": version,
        "required_correct": required_correct,
        "questions": [
            {"id": q.id, "prompt": q.prompt, "choices": list(q.choices_json), "correct_index": q.correct_index}
            for q in picked
        ],
    }
    session.add(
        VerifyQuizSession(
            token=token,
            question_ids_json=ids,
            shuffle_maps_json=shuffle_maps,
        )
    )
    await session.flush()
    return DrawnQuiz(questions=picked, shuffle_maps=shuffle_maps, version=version, required_correct=required_correct)


async def grade_answers(
    session: AsyncSession,
    *,
    token: str,
    answers: dict[int, int],
    quiz_version: str | None = None,
) -> tuple[int, int, int]:
    """Return (num_correct, num_questions, required_correct) from the version the browser displayed."""
    sess = await session.get(VerifyQuizSession, token)
    if sess is None:
        raise QuizChangedError("quiz session missing")
    drawn = quiz_from_snapshot(sess)
    if drawn is None or quiz_version != drawn.version:
        raise QuizChangedError("quiz snapshot changed")
    correct_count = 0
    total = 0
    for q in drawn.questions:
        qid = q.id
        total += 1
        n = len(q.choices_json)
        order = normalize_shuffle_order(drawn.shuffle_maps, int(qid), n)
        ans = answers.get(qid)
        if ans is None:
            continue
        if ans < 0 or ans >= len(order):
            continue
        orig_pick = order[ans]
        if orig_pick == q.correct_index:
            correct_count += 1
    return correct_count, total, drawn.required_correct
